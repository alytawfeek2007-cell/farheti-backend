"""
Scheduled jobs. Plain functions, called by the management commands
(expire_bookings, check_refunds), so any scheduler can run them.

Lock order is respected: every job locks Booking (or only RefundAttempt),
never in the opposite order. Every booking is its own transaction, so one
failure never blocks the rest.
"""
import logging
from datetime import timedelta

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from . import emails, payments
from .models import MIN_DAYS_AHEAD_TO_ACCEPT, Booking, RefundAttempt

job_logger = logging.getLogger('farheti.jobs')
money_logger = logging.getLogger('farheti.payments')

S = RefundAttempt.Status

BATCH_LIMIT = 500                 # max rows handled per pass; the next run continues
PENDING_MAX_AGE_HOURS = 72        # provider has 72h to answer a request
AUTO_RESUME_MAX_ATTEMPTS = 20     # stop auto-finishing a GATEWAY_REFUNDED attempt after this
ALERT_REPEAT_SECONDS = 6 * 3600   # re-log the same stuck refund at most every 6 hours
CLEARED_REPORT_AFTER_HOURS = 24   # an owner-cleared FAILED attempt is reported again after this
MAX_STUCK_LINES = 100             # cap on listed/alerted attempts per run
REMINDER_HOURS_BEFORE_DEADLINE = 6  # one reminder when this much of the 24h payment window is left

# ---------------------------------------------------------------- booking expiry
def _run_transitions(ids, transition, label, dry_run):
    result = {'candidates': len(ids), 'ids': ids, 'done': 0, 'skipped': 0, 'failed': 0}
    if dry_run:
        return result
    for pk in ids:
        try:
            transition(Booking.objects.get(pk=pk))
            result['done'] += 1
            job_logger.info('%s: booking %s', label, pk)
        except ValidationError as exc:
            # The booking changed state since we queried (paid, cancelled...). Fine.
            result['skipped'] += 1
            job_logger.info('%s: booking %s skipped (%s)', label, pk, '; '.join(exc.messages))
        except Exception:
            result['failed'] += 1
            job_logger.exception('%s: booking %s FAILED', label, pk)
    return result


def expire_unpaid_bookings(*, dry_run=False, limit=BATCH_LIMIT):
    """ACCEPTED bookings whose payment deadline has passed -> EXPIRED."""
    ids = list(
        Booking.objects.filter(status=Booking.Status.ACCEPTED, payment_deadline__lt=timezone.now())
        .order_by('payment_deadline')
        .values_list('pk', flat=True)[:limit]
    )
    return _run_transitions(ids, lambda b: b.mark_expired(), 'expired unpaid', dry_run)


def expire_stale_pending_bookings(*, dry_run=False, limit=BATCH_LIMIT):
    """PENDING bookings the provider ignored for too long, or that can no longer be paid in time."""
    older_than = timezone.now() - timedelta(hours=PENDING_MAX_AGE_HOURS)
    event_before = timezone.localdate() + timedelta(days=MIN_DAYS_AHEAD_TO_ACCEPT)
    stale = Q(created_at__lt=older_than) | Q(event_date__lt=event_before)
    ids = list(
        Booking.objects.filter(status=Booking.Status.PENDING)
        .filter(stale)
        .order_by('created_at')
        .values_list('pk', flat=True)[:limit]
    )
    return _run_transitions(
        ids,
        lambda b: b.mark_expired_unanswered(older_than=older_than, event_before=event_before),
        'expired unanswered',
        dry_run,
    )


# ---------------------------------------------------------------- refund housekeeping
def _cutoff():
    return timezone.now() - timedelta(seconds=payments.IN_FLIGHT_STALE_SECONDS)


def _park_stale_in_flight():
    """Abandoned IN_FLIGHT claims -> UNKNOWN (never re-sent automatically). Returns (parked, failed)."""
    ids = list(
        RefundAttempt.objects.filter(status=S.IN_FLIGHT, started_at__lt=_cutoff())
        .values_list('pk', flat=True)[:BATCH_LIMIT]
    )
    parked = failed = 0
    for pk in ids:
        try:
            with transaction.atomic():
                attempt = RefundAttempt.objects.select_for_update().get(pk=pk)
                if payments._park_if_unresolved(attempt):
                    parked += 1
        except Exception:
            failed += 1
            money_logger.exception('check_refunds: could not park attempt %s', pk)
    return parked, failed


def _resume_gateway_refunded():
    """
    Paymob already refunded these; only the DB side is unfinished, so finishing is safe to
    automate. Skips ones that already failed to finalize (RECONCILE) or tried too often.
    Returns (finished, skipped, failed). "skipped" = another run or an admin got there first.
    """
    ids = list(
        RefundAttempt.objects.filter(
            Q(started_at__lt=_cutoff()) | Q(started_at__isnull=True),
            status=S.GATEWAY_REFUNDED,
            attempts__lt=AUTO_RESUME_MAX_ATTEMPTS,
        )
        .exclude(last_error__startswith='RECONCILE')
        .values_list('pk', flat=True)[:BATCH_LIMIT]
    )
    finished = skipped = failed = 0
    for pk in ids:
        try:
            attempt = payments.retry_refund_attempt(attempt_id=pk, actor=None)
            if attempt.status == S.DONE:
                finished += 1
        except payments.PaymentFlowError as exc:
            current = RefundAttempt.objects.filter(pk=pk).first()
            if current is not None and current.last_error.startswith('RECONCILE'):
                # Paymob refunded but the booking could not be finalized: a real failure, even
                # though this run's own fresh claim makes the attempt look "busy".
                failed += 1
                money_logger.error(
                    'check_refunds: attempt %s refunded at Paymob but the booking could not be finalized: %s',
                    pk, exc,
                )
            elif current is None or current.status == S.DONE or payments._busy(current):
                skipped += 1  # healthy race: finished or being worked on by someone else
            else:
                failed += 1
                money_logger.error('check_refunds: could not finish attempt %s: %s', pk, exc)
        except Exception:
            failed += 1
            money_logger.exception('check_refunds: unexpected error finishing attempt %s', pk)
    return finished, skipped, failed


def _stuck_queryset():
    now = timezone.now()
    not_fresh = Q(started_at__lt=_cutoff()) | Q(started_at__isnull=True)
    cleared_recently = Q(last_error__startswith=payments.CLEARED_NOT_REFUNDED_PREFIX) & Q(
        updated_at__gte=now - timedelta(hours=CLEARED_REPORT_AFTER_HOURS)
    )
    return (
        RefundAttempt.objects.filter(
            Q(status=S.UNKNOWN)
            | (Q(status=S.FAILED) & ~cleared_recently)
            | (Q(status__in=[S.GATEWAY_REFUNDED, S.IN_FLIGHT]) & not_fresh)
        )
        .select_related('payment')
        .order_by('created_at')
    )


def _is_resumable(attempt):
    return (
        attempt.status == S.GATEWAY_REFUNDED
        and attempt.attempts < AUTO_RESUME_MAX_ATTEMPTS
        and not attempt.last_error.startswith('RECONCILE')
    )


def _describe(attempt, dry_run=False):
    note = ''
    if dry_run:
        if attempt.status == S.IN_FLIGHT and attempt.started_at is not None:
            note = 'WOULD PARK AS UNKNOWN: '
        elif _is_resumable(attempt):
            note = 'WOULD RESUME: '
    return (
        f'{note}attempt #{attempt.pk} booking #{attempt.payment.booking_id} {attempt.kind} '
        f'status={attempt.status} amount={attempt.amount} EGP txn={attempt.paymob_transaction_id} '
        f'tries={attempt.attempts} error={attempt.last_error or "-"}'
    )


def _alert_once(attempt, line):
    """CRITICAL log at most once per ALERT_REPEAT_SECONDS per attempt+status. Returns True if logged."""
    key = f'stuck-refund:{attempt.pk}:{attempt.status}'
    try:
        first = cache.add(key, 1, ALERT_REPEAT_SECONDS)
    except Exception:
        first = True  # cache trouble must never hide a money problem
    if first:
        money_logger.critical('STUCK REFUND NEEDS THE OWNER: %s', line)
    return first


def process_refunds(*, dry_run=False):
    """Park, auto-finish what is safe, then report whatever still needs the owner."""
    parked = finished = skipped = failed = 0
    if not dry_run:
        parked, park_failed = _park_stale_in_flight()
        finished, skipped, resume_failed = _resume_gateway_refunded()
        failed = park_failed + resume_failed
    queryset = _stuck_queryset()
    stuck_total = queryset.count()
    stuck, alerted = [], []
    alerted_total = 0
    # Alert over EVERY stuck attempt (the cache dedupes); only the printed lines are capped.
    for attempt in queryset.iterator(chunk_size=200):
        line = _describe(attempt, dry_run)
        if len(stuck) < MAX_STUCK_LINES:
            stuck.append(line)
        if not dry_run and _alert_once(attempt, line):
            alerted_total += 1
            if len(alerted) < MAX_STUCK_LINES:
                alerted.append(line)
    if alerted:
        emails.send_owner_alert(alerted, alerted_total)                
    return {
        'parked': parked, 'finished': finished, 'skipped': skipped, 'failed': failed,
        'stuck_total': stuck_total, 'stuck': stuck,
        'alerted': alerted, 'alerted_total': alerted_total,
    }

# ---------------------------------------------------------------- payment reminders
def _remind_one(pk, window_end):
    """Claim first, send after commit: a crash can lose a reminder but can never send two."""
    with transaction.atomic():
        booking = Booking.objects.select_for_update().get(pk=pk)
        now = timezone.now()
        if (
            booking.status != Booking.Status.ACCEPTED
            or booking.deadline_reminder_sent_at is not None
            or booking.payment_deadline is None
            or not (now < booking.payment_deadline <= window_end)
            or booking.event_date <= timezone.localdate()
        ):
            return False
        booking.deadline_reminder_sent_at = now
        booking.save(update_fields=['deadline_reminder_sent_at'])
        emails.notify_booking(pk, 'reminder')
    return True


def send_payment_reminders(*, dry_run=False, limit=BATCH_LIMIT):
    """One reminder per ACCEPTED booking whose payment window closes within the next few hours."""
    now = timezone.now()
    window_end = now + timedelta(hours=REMINDER_HOURS_BEFORE_DEADLINE)
    ids = list(
        Booking.objects.filter(
            status=Booking.Status.ACCEPTED,
            deadline_reminder_sent_at__isnull=True,
            payment_deadline__gt=now,
            payment_deadline__lte=window_end,
            event_date__gt=timezone.localdate(),
            provider_service__provider__is_suspended=False,
            provider_service__provider__user__is_active=True,
        )
        .order_by('payment_deadline')
        .values_list('pk', flat=True)[:limit]
    )
    result = {'candidates': len(ids), 'ids': ids, 'done': 0, 'skipped': 0, 'failed': 0}
    if dry_run:
        return result
    for pk in ids:
        try:
            if _remind_one(pk, window_end):
                result['done'] += 1
                job_logger.info('payment reminder: booking %s', pk)
            else:
                result['skipped'] += 1
        except Exception:
            result['failed'] += 1
            job_logger.exception('payment reminder: booking %s FAILED', pk)
    return result    