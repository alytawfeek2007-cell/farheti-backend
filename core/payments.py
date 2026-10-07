import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from django.contrib.admin.models import CHANGE, LogEntry
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.utils import timezone

from . import emails, paymob
from .models import (
    Booking, Payment, PaymentAttempt, PaymentNotConfirmable,
    Provider, ProviderService, RefundAttempt,
)

logger = logging.getLogger('farheti.payments')

CUSTOMER_REFUND_MIN_DAYS = 7
ATTEMPT_REUSE_SECONDS = 600
CREATING_WINDOW_SECONDS = 30
IN_FLIGHT_STALE_SECONDS = 120
MAX_ATTEMPTS_PER_PAYMENT = 20
MAX_INTENTION_SECONDS = 3600
MIN_INTENTION_SECONDS = 60
FALLBACK_PHONE = '+201000000000'
CLEARED_NOT_REFUNDED_PREFIX = 'cleared by owner'

S = RefundAttempt.Status
K = RefundAttempt.Kind
# Cancellation attempts in these states block payout (money may have moved or be moving).
PAYOUT_BLOCKING = (S.IN_FLIGHT, S.UNKNOWN, S.GATEWAY_REFUNDED, S.DONE)


class PaymentFlowError(Exception):
    pass


class FlowNotFound(PaymentFlowError):
    pass


class FlowForbidden(PaymentFlowError):
    pass


class FlowConflict(PaymentFlowError):
    pass


class FlowGatewayDown(PaymentFlowError):
    pass


class FlowRefundIncomplete(PaymentFlowError):
    pass


@dataclass(frozen=True)
class Checkout:
    url: str
    reused: bool


def _audit(actor, obj, message):
    if actor is not None and getattr(actor, 'is_staff', False):
        LogEntry.objects.log_actions(
            user_id=actor.pk, queryset=[obj], action_flag=CHANGE, change_message=message
        )


def _fresh(attempt):
    return bool(attempt.started_at) and (
        timezone.now() - attempt.started_at < timedelta(seconds=IN_FLIGHT_STALE_SECONDS)
    )


def _busy(attempt):
    """Someone else is actively working on this attempt right now."""
    return attempt.status in (S.IN_FLIGHT, S.GATEWAY_REFUNDED) and _fresh(attempt)


def _provider_blocked(provider):
    return provider.is_suspended or not provider.user.is_active


def _lock_chain(booking_id):
    """Provider -> ProviderService -> Booking. Call inside transaction.atomic()."""
    ids = (
        Booking.objects.filter(pk=booking_id)
        .values_list('provider_service_id', 'provider_service__provider_id')
        .first()
    )
    if ids is None:
        raise FlowNotFound()
    ps_id, provider_id = ids
    provider = (
        Provider.objects.select_related('user')
        .select_for_update(of=('self',))
        .get(pk=provider_id)
    )
    ProviderService.objects.select_for_update().get(pk=ps_id)
    booking = Booking.objects.select_for_update().get(pk=booking_id)
    return provider, booking


def _set_attempt(attempt_id, *, token=None, **fields):
    """
    Update under the row lock. `token` is the started_at of the caller's claim: if the claim was
    taken over meanwhile, write nothing and return False.
    """
    if 'last_error' in fields:
        fields['last_error'] = (fields['last_error'] or '')[:255]
    with transaction.atomic():
        attempt = RefundAttempt.objects.select_for_update().get(pk=attempt_id)
        if token is not None and attempt.started_at != token:
            logger.critical('LOST CLAIM on refund attempt %s: update %s dropped', attempt_id, fields)
            return False
        for key, value in fields.items():
            setattr(attempt, key, value)
        attempt.save(update_fields=[*fields, 'updated_at'])
        return True


def _claim(attempt):
    """Caller holds the attempt row lock inside an atomic block. GATEWAY_REFUNDED keeps its status."""
    if attempt.status != S.GATEWAY_REFUNDED:
        attempt.status = S.IN_FLIGHT
    attempt.started_at = timezone.now()
    attempt.attempts += 1
    attempt.save(update_fields=['status', 'started_at', 'attempts', 'updated_at'])

def _park_if_unresolved(attempt):
    """
    Under the attempt row lock. A claimed IN_FLIGHT attempt that is no longer fresh was abandoned
    mid-call, so Paymob's outcome is unknown: park it as UNKNOWN (never re-send automatically).
    Returns True if the attempt now needs the owner's review.
    """
    if attempt.status == S.IN_FLIGHT and attempt.started_at is not None and not _fresh(attempt):
        attempt.status = S.UNKNOWN
        attempt.last_error = 'Claim abandoned mid-call; Paymob outcome unknown. Check the Paymob dashboard.'
        attempt.save(update_fields=['status', 'last_error', 'updated_at'])
        logger.error('REFUND NEEDS ATTENTION attempt=%s parked as UNKNOWN (abandoned claim)', attempt.pk)
    return attempt.status == S.UNKNOWN

# ---------------------------------------------------------------- checkout initiation
def start_checkout(*, booking_id, user, phone=None):
    # Unlocked ownership check first: non-owners never take row locks.
    if not Booking.objects.filter(pk=booking_id, customer=user).exists():
        raise FlowNotFound()
    now = timezone.now()
    with transaction.atomic():
        provider, booking = _lock_chain(booking_id)
        if booking.customer_id != user.pk:
            raise FlowNotFound()
        if _provider_blocked(provider):
            raise FlowConflict('This booking cannot be paid right now. Please contact support.')
        if booking.status != Booking.Status.ACCEPTED:
            raise FlowConflict('This booking is not awaiting payment.')
        if booking.payment_deadline is None or booking.is_payment_expired:
            raise FlowConflict('The payment deadline for this booking has passed.')
        if booking.event_date <= timezone.localdate():
            raise FlowConflict('The event date has arrived or passed; this booking can no longer be paid.')
        remaining = int((booking.payment_deadline - now).total_seconds())
        if remaining < MIN_INTENTION_SECONDS:
            raise FlowConflict('The payment deadline for this booking is about to pass.')

        payment, _ = Payment.objects.get_or_create(booking=booking)
        payment = Payment.objects.select_for_update().get(pk=payment.pk)
        if payment.paid_at is not None or payment.refunded_at is not None:
            raise FlowConflict('This booking has already been paid.')

        reusable = (
            payment.attempts.exclude(client_secret='')
            .filter(created_at__gte=now - timedelta(seconds=ATTEMPT_REUSE_SECONDS))
            .order_by('-created_at')
            .first()
        )
        if reusable is not None:
            return Checkout(url=paymob.checkout_url(reusable.client_secret), reused=True)
        if payment.attempts.filter(
            client_secret='', created_at__gte=now - timedelta(seconds=CREATING_WINDOW_SECONDS)
        ).exists():
            raise FlowConflict('A payment session is being created. Please try again in a few seconds.')
        if payment.attempts.exclude(client_secret='').count() >= MAX_ATTEMPTS_PER_PAYMENT:
            raise FlowConflict('Too many payment attempts for this booking. Please contact support.')

        attempt = PaymentAttempt.objects.create(
            payment=payment, special_reference=f'fh-{payment.pk}-{uuid.uuid4().hex[:16]}'
        )
        amount_cents = paymob.to_cents(payment.amount)
        billing = {
            'first_name': (user.first_name or 'Farheti').strip()[:50] or 'Farheti',
            'last_name': (user.last_name or 'Customer').strip()[:50] or 'Customer',
            'email': user.email,
            'phone_number': phone or FALLBACK_PHONE,
            'country': 'EG',
        }
        item_name = f'Farheti booking #{booking.pk}'
        expiration = max(MIN_INTENTION_SECONDS, min(remaining, MAX_INTENTION_SECONDS))
        reference = attempt.special_reference
        attempt_pk = attempt.pk

    # Outside every lock: the network call.
    try:
        intention = paymob.create_intention(
            amount_cents=amount_cents, special_reference=reference, booking_id=booking_id,
            item_name=item_name, billing=billing, expiration_seconds=expiration,
        )
    except paymob.PaymobError as exc:
        logger.error('Paymob intention failed for booking %s: %s', booking_id, exc)
        PaymentAttempt.objects.filter(pk=attempt_pk, paymob_order_id__isnull=True).delete()
        raise FlowGatewayDown() from exc

    PaymentAttempt.objects.filter(pk=attempt_pk).update(
        paymob_order_id=intention.order_id,
        paymob_intention_id=intention.intention_id,
        client_secret=intention.client_secret,
    )
    return Checkout(url=paymob.checkout_url(intention.client_secret), reused=False)


# ---------------------------------------------------------------- webhook handling
def handle_successful_charge(*, payment, txn_id, amount_cents, currency):
    txn = str(txn_id)
    amount = (Decimal(amount_cents) / Decimal(100)).quantize(Decimal('0.01'))
    reason = None
    if currency != 'EGP':
        reason = f'unexpected currency {currency!r}'
    else:
        event_date = Booking.objects.values_list('event_date', flat=True).get(pk=payment.booking_id)
        if event_date <= timezone.localdate():
            reason = 'event date has arrived or passed'
    if reason is None:
        try:
            if payment.mark_paid_and_confirm_booking(txn, amount):
                emails.notify_booking(payment.booking_id, 'paid')
            return 'confirmed'
        except PaymentNotConfirmable as exc:
            reason = str(exc)
    logger.warning('Charge %s cannot confirm payment %s: %s', txn, payment.pk, reason)
    status = refund_unconfirmed_charge(payment_id=payment.pk, txn_id=txn, amount=amount, reason=reason)
    if status is None:
        return 'already_confirmed'  # redelivery of the legitimate charge: nothing to do
    return f'refund_{status}'


def refund_unconfirmed_charge(*, payment_id, txn_id, amount, reason):
    txn = str(txn_id)
    booking_id = Payment.objects.values_list('booking_id', flat=True).get(pk=payment_id)
    with transaction.atomic():
        Booking.objects.select_for_update().get(pk=booking_id)
        payment = Payment.objects.select_for_update().get(pk=payment_id)
        if payment.paymob_transaction_id == txn:
            return None  # never refund the legitimate confirmed charge through this path
        attempt, _ = RefundAttempt.objects.get_or_create(
            paymob_transaction_id=txn,
            defaults={'payment': payment, 'kind': K.UNCONFIRMED, 'amount': amount, 'last_error': reason[:255]},
        )
        attempt = RefundAttempt.objects.select_for_update().get(pk=attempt.pk)
        if attempt.status == S.DONE or _busy(attempt):
            return attempt.status
        if _park_if_unresolved(attempt):
            return attempt.status
        _claim(attempt)
        attempt_pk = attempt.pk
    _drive_attempt(attempt_pk)
    return RefundAttempt.objects.values_list('status', flat=True).get(pk=attempt_pk)


# ---------------------------------------------------------------- refund engine
def _drive_attempt(attempt_id):
    """Gateway call (no locks held) then finalize. Safe to re-run: resumes from GATEWAY_REFUNDED."""
    attempt = RefundAttempt.objects.select_related('payment').get(pk=attempt_id)
    token = attempt.started_at
    if attempt.status != S.GATEWAY_REFUNDED:
        try:
            txn_int = int(attempt.paymob_transaction_id)
        except ValueError:
            _set_attempt(attempt_id, token=token, status=S.FAILED, last_error='non-numeric transaction id')
            return
        result = paymob.refund_transaction(
            transaction_id=txn_int, amount_cents=paymob.to_cents(attempt.amount)
        )
        if not result.ok:
            # A repeat attempt may be rejected only because an earlier one already succeeded
            # (lost response / crash), so after the first try never trust "definitive".
            definitive = result.definitive and attempt.attempts <= 1
            _set_attempt(
                attempt_id, token=token, status=S.FAILED if definitive else S.UNKNOWN, last_error=result.error
            )
            logger.error(
                'REFUND NEEDS ATTENTION attempt=%s txn=%s kind=%s tries=%s error=%s',
                attempt_id, attempt.paymob_transaction_id, attempt.kind, attempt.attempts, result.error,
            )
            return
        reference = result.reference[:150]
        if not _set_attempt(
            attempt_id, token=token, status=S.GATEWAY_REFUNDED, refund_reference=reference, last_error=''
        ):
            logger.critical(
                'REFUND WAS SENT at Paymob (reference=%s) for attempt %s but the claim was lost; reconcile manually',
                reference, attempt_id,
            )
            return
        attempt.refund_reference = reference
    _finalize_attempt(attempt)


def _finalize_attempt(attempt):
    payment = attempt.payment
    reference = attempt.refund_reference
    token = attempt.started_at
    if attempt.kind == K.UNCONFIRMED:
        payment.record_refunded_charge(attempt.paymob_transaction_id, attempt.amount, reference)
    else:
        if Payment.objects.get(pk=payment.pk).refunded_at is None:
            try:
                Booking.objects.get(pk=payment.booking_id).mark_cancelled_and_refunded(reference)
            except DjangoValidationError as exc:
                # A concurrent finalizer may have just completed it: that is success, not an error.
                if not Payment.objects.filter(pk=payment.pk, refunded_at__isnull=False).exists():
                    message = '; '.join(exc.messages)
                    _set_attempt(attempt.pk, token=token, last_error=f'RECONCILE: {message}')
                    logger.critical(
                        'REFUND SENT BUT BOOKING NOT FINALIZED attempt=%s booking=%s: %s',
                        attempt.pk, payment.booking_id, message,
                    )
                    raise FlowConflict(
                        'The refund was sent but the booking could not be finalized. Support has been alerted.'
                    ) from exc
    _set_attempt(attempt.pk, token=token, status=S.DONE)

def cancel_and_refund(*, booking_id, actor, role):
    """role: 'customer' | 'provider' | 'staff'. Full refund of a PAID booking, then cancel it."""
    blocked = None
    with transaction.atomic():
        _, booking = _lock_chain(booking_id)
        try:
            payment = Payment.objects.select_for_update().get(booking=booking)
        except Payment.DoesNotExist:
            raise FlowConflict('This booking has no payment to refund.')
        if booking.status != Booking.Status.PAID or payment.paid_at is None:
            raise FlowConflict('Only paid bookings can be cancelled with a refund.')
        if payment.refunded_at is not None:
            raise FlowConflict('This payment has already been refunded.')
        if payment.payout_completed:
            raise FlowConflict('The payout for this booking has already been made. Please contact support.')
        if not payment.paymob_transaction_id:
            raise FlowConflict('This payment cannot be refunded automatically. Please contact support.')

        existing = RefundAttempt.objects.select_for_update().filter(
            paymob_transaction_id=payment.paymob_transaction_id
        ).first()
        # Money already sent to Paymob: finishing is DB-only, so the date/role policy no longer applies.
        resuming = existing is not None and existing.status == S.GATEWAY_REFUNDED
        if not resuming:
            today = timezone.localdate()
            if role == 'customer' and (booking.event_date - today).days < CUSTOMER_REFUND_MIN_DAYS:
                raise FlowForbidden(
                    f'Bookings can be cancelled with a refund only {CUSTOMER_REFUND_MIN_DAYS} or more days '
                    'before the event. Please contact support.'
                )
            if role == 'provider' and booking.event_date <= today:
                raise FlowForbidden('The event date has arrived. Please contact support.')

        if existing is None:
            requester = actor if getattr(actor, 'is_authenticated', False) else None
            attempt = RefundAttempt.objects.create(
                payment=payment, kind=K.CANCELLATION, amount=payment.amount,
                paymob_transaction_id=payment.paymob_transaction_id, requested_by=requester,
            )
        else:
            attempt = existing
            if attempt.status == S.DONE:
                blocked = 'This payment has already been refunded.'
            elif _busy(attempt):
                blocked = 'A refund for this booking is already being processed.'
            elif _park_if_unresolved(attempt):
                blocked = (
                    'The refund outcome is unknown. Check the Paymob dashboard, then use "Record manually" '
                    'or "Mark as NOT refunded" in admin.'
                    if role == 'staff' else 'This refund needs support review. Please contact support.'
                )
            elif attempt.status == S.FAILED and role != 'staff':
                blocked = 'The refund could not be completed. Please contact support.'
        if blocked is None:
            _claim(attempt)
            _audit(actor, booking, f'Cancel-and-refund started ({role}); attempt #{attempt.pk}')
        attempt_pk = attempt.pk

    if blocked is not None:
        raise FlowConflict(blocked)  # raised outside the transaction so a parked UNKNOWN is committed

    _drive_attempt(attempt_pk)
    final = RefundAttempt.objects.get(pk=attempt_pk)
    if final.status != S.DONE:
        raise FlowRefundIncomplete(
            'The refund could not be completed right now. The booking was not changed and support has been alerted.'
        )
    _audit(actor, Booking.objects.get(pk=booking_id), f'Cancel-and-refund completed; refund {final.refund_reference}')
    return final


def retry_refund_attempt(*, attempt_id, actor=None):
    info = RefundAttempt.objects.filter(pk=attempt_id).values_list('payment_id', 'payment__booking_id').first()
    if info is None:
        raise FlowNotFound()
    payment_id, booking_id = info
    blocked = None
    with transaction.atomic():
        booking = Booking.objects.select_for_update().get(pk=booking_id)
        payment = Payment.objects.select_for_update().get(pk=payment_id)
        attempt = RefundAttempt.objects.select_for_update().get(pk=attempt_id)
        if attempt.status == S.DONE:
            blocked = 'This refund is already completed.'
        elif _busy(attempt):
            blocked = 'This refund is currently being processed.'
        elif _park_if_unresolved(attempt):
            blocked = (
                'Outcome unknown: check the Paymob dashboard. If the charge was refunded use "Record manually"; '
                'otherwise run "Mark as NOT refunded" first, then retry.'
            )
        elif attempt.status != S.GATEWAY_REFUNDED and attempt.kind == K.CANCELLATION and (
            booking.status != Booking.Status.PAID or payment.refunded_at is not None or payment.payout_completed
        ):
            blocked = 'This booking can no longer be cancelled with a refund.'
        else:
            _claim(attempt)
            _audit(actor, attempt, 'Refund retried')
    if blocked is not None:
        raise FlowConflict(blocked)
    _drive_attempt(attempt_id)
    return RefundAttempt.objects.get(pk=attempt_id)


def resolve_attempt_manually(*, attempt_id, reference, actor):
    """Owner refunded the charge by hand in the Paymob dashboard; finish the DB side."""
    reference = (reference or '').strip()
    if not reference or len(reference) > 150:
        raise FlowConflict('A refund reference (1-150 characters) is required.')
    info = RefundAttempt.objects.filter(pk=attempt_id).values_list('payment_id', 'payment__booking_id').first()
    if info is None:
        raise FlowNotFound()
    payment_id, booking_id = info
    with transaction.atomic():
        Booking.objects.select_for_update().get(pk=booking_id)
        Payment.objects.select_for_update().get(pk=payment_id)
        attempt = RefundAttempt.objects.select_for_update().get(pk=attempt_id)
        if attempt.status == S.DONE:
            raise FlowConflict('This refund is already completed.')
        if _busy(attempt):
            raise FlowConflict('A refund is currently being processed. Try again in two minutes.')
        attempt.status = S.GATEWAY_REFUNDED
        attempt.refund_reference = reference
        attempt.last_error = 'resolved manually'
        attempt.started_at = timezone.now()
        attempt.save(update_fields=['status', 'refund_reference', 'last_error', 'started_at', 'updated_at'])
        _audit(actor, attempt, f'Refund recorded manually; reference {reference}')
    _finalize_attempt(RefundAttempt.objects.select_related('payment').get(pk=attempt_id))
    return RefundAttempt.objects.get(pk=attempt_id)


def mark_attempt_not_refunded(*, attempt_id, actor):
    """Owner verified in the Paymob dashboard that NO refund happened: clear UNKNOWN/FAILED to a clean FAILED."""
    info = RefundAttempt.objects.filter(pk=attempt_id).values_list('payment_id', 'payment__booking_id').first()
    if info is None:
        raise FlowNotFound()
    payment_id, booking_id = info
    with transaction.atomic():
        Booking.objects.select_for_update().get(pk=booking_id)
        Payment.objects.select_for_update().get(pk=payment_id)
        attempt = RefundAttempt.objects.select_for_update().get(pk=attempt_id)
        if attempt.status not in (S.UNKNOWN, S.FAILED):
            raise FlowConflict('Only unknown or failed refund attempts can be cleared.')
        attempt.status = S.FAILED
        attempt.attempts = 0
        attempt.last_error = f'{CLEARED_NOT_REFUNDED_PREFIX}: verified not refunded at Paymob'
        attempt.save(update_fields=['status', 'attempts', 'last_error', 'updated_at'])
        _audit(actor, attempt, 'Marked as NOT refunded (owner verified in Paymob)')
    return attempt


# ---------------------------------------------------------------- payout marking (admin only)
def mark_payout_done(*, payment_id, reference, actor):
    reference = (reference or '').strip()
    if not reference or len(reference) > 150:
        raise FlowConflict('A payout reference (1-150 characters) is required.')
    booking_id = Payment.objects.filter(pk=payment_id).values_list('booking_id', flat=True).first()
    if booking_id is None:
        raise FlowNotFound()
    with transaction.atomic():
        provider, booking = _lock_chain(booking_id)
        payment = Payment.objects.select_for_update().get(pk=payment_id)
        if payment.paid_at is None:
            raise FlowConflict('This payment was never paid.')
        if payment.refunded_at is not None:
            raise FlowConflict('This payment was refunded.')
        if payment.payout_completed:
            raise FlowConflict('The payout was already marked as done.')
        if _provider_blocked(provider):
            raise FlowConflict('The provider is suspended or deactivated; payouts are on hold.')
        if booking.status not in (Booking.Status.PAID, Booking.Status.COMPLETED):
            raise FlowConflict(f"Booking status is '{booking.status}'; payout is not allowed.")
        if booking.status == Booking.Status.PAID and booking.event_date >= timezone.localdate():
            raise FlowConflict('The event date has not passed yet.')
        if RefundAttempt.objects.filter(payment=payment, kind=K.CANCELLATION, status__in=PAYOUT_BLOCKING).exists():
            raise FlowConflict('A cancellation refund exists or is in progress for this payment.')
        payment.payout_completed = True
        payment.payout_completed_at = timezone.now()
        payment.payout_reference = reference
        payment.save(update_fields=['payout_completed', 'payout_completed_at', 'payout_reference'])
        _audit(actor, payment, f'Payout marked done; reference {reference}')
    return payment