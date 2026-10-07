from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from core.models import (
    MIN_DAYS_AHEAD_TO_ACCEPT, Booking, Payment, PaymentNotConfirmable, Provider,
    ProviderService, RefundedCharge,
)

from .helpers import (
    BREAK_MODES, B, K, S, FarhetiTestCase, break_listing, make_booking, make_listing,
    make_paid_booking, make_refund_attempt,
)


class MarkPaidAndConfirmTests(FarhetiTestCase):
    def setUp(self):
        super().setUp()
        self.booking = make_booking(status=B.ACCEPTED)
        self.payment = Payment.objects.create(booking=self.booking)

    def confirm(self, txn='1001', amount='1000.00'):
        self.payment.mark_paid_and_confirm_booking(txn, Decimal(amount))

    def reload(self):
        self.booking.refresh_from_db()
        self.payment.refresh_from_db()

    def test_success_confirms_payment_and_booking_together(self):
        self.confirm()
        self.reload()
        self.assertEqual(self.booking.status, B.PAID)
        self.assertIsNotNone(self.payment.paid_at)
        self.assertTrue(self.payment.is_paid)
        self.assertEqual(self.payment.paymob_transaction_id, '1001')

    def test_amount_must_be_a_decimal(self):
        for bad in (100000, 1000.0, '1000.00'):
            with self.subTest(value=bad):
                with self.assertRaises(TypeError):
                    self.payment.mark_paid_and_confirm_booking('1001', bad)
        self.reload()
        self.assertEqual(self.booking.status, B.ACCEPTED)
        self.assertIsNone(self.payment.paid_at)

    def test_redelivered_webhook_is_a_silent_no_op(self):
        self.confirm()
        self.reload()
        first_paid_at = self.payment.paid_at
        self.confirm()  # same txn again: must not raise
        self.reload()
        self.assertEqual(self.payment.paid_at, first_paid_at)
        self.assertEqual(self.booking.status, B.PAID)

    def test_redelivery_is_still_a_no_op_after_booking_moved_on(self):
        self.confirm()
        Booking.objects.filter(pk=self.booking.pk).update(status=B.COMPLETED)
        self.confirm()  # idempotency check comes before the status check
        self.reload()
        self.assertEqual(self.booking.status, B.COMPLETED)

    def test_wrong_booking_status_is_rejected(self):
        for status in (B.PENDING, B.DECLINED, B.EXPIRED, B.CANCELLED, B.COMPLETED, B.PAID):
            with self.subTest(status=status):
                Booking.objects.filter(pk=self.booking.pk).update(status=status)
                with self.assertRaises(PaymentNotConfirmable):
                    self.confirm()
                self.reload()
                self.assertIsNone(self.payment.paid_at)
                self.assertEqual(self.booking.status, status)

    def test_expired_deadline_is_rejected(self):
        Booking.objects.filter(pk=self.booking.pk).update(
            payment_deadline=timezone.now() - timedelta(minutes=1)
        )
        with self.assertRaisesMessage(PaymentNotConfirmable, 'deadline'):
            self.confirm()
        self.reload()
        self.assertEqual(self.booking.status, B.ACCEPTED)
        self.assertIsNone(self.payment.paid_at)

    def test_amount_mismatch_is_rejected(self):
        for amount in ('999.99', '1000.01', '0.01', '100000.00'):
            with self.subTest(amount=amount):
                with self.assertRaisesMessage(PaymentNotConfirmable, 'Amount mismatch'):
                    self.confirm(amount=amount)
        self.reload()
        self.assertEqual(self.booking.status, B.ACCEPTED)
        self.assertIsNone(self.payment.paid_at)

    def test_already_refunded_payment_is_rejected(self):
        now = timezone.now()
        Payment.objects.filter(pk=self.payment.pk).update(
            paid_at=now, refunded_at=now, paymob_transaction_id='old'
        )
        with self.assertRaisesMessage(PaymentNotConfirmable, 'already been refunded'):
            self.confirm(txn='1002')

    def test_a_second_different_charge_on_a_confirmed_payment_is_rejected(self):
        self.confirm('1001')
        with self.assertRaises(PaymentNotConfirmable):
            self.confirm('1002')
        self.reload()
        self.assertEqual(self.payment.paymob_transaction_id, '1001')

    def test_exception_is_not_a_validation_error(self):
        # A broad `except ValidationError` elsewhere must never swallow it and trigger a refund.
        self.assertFalse(issubclass(PaymentNotConfirmable, ValidationError))

    def test_payment_amount_is_the_booking_snapshot_not_the_live_price(self):
        ProviderService.objects.filter(pk=self.booking.provider_service_id).update(price=Decimal('1.00'))
        self.assertEqual(self.payment.amount, Decimal('1000.00'))
        self.confirm()  # still confirms at the snapshot amount


class MoneyMathTests(FarhetiTestCase):
    def test_commission_rounds_half_up(self):
        booking = make_booking(price='100.05', commission='10.00')
        payment = Payment.objects.create(booking=booking)
        self.assertEqual(payment.commission_amount, Decimal('10.01'))  # 10.005 -> 10.01
        self.assertEqual(payment.payout_amount, Decimal('90.04'))

    def test_zero_and_full_commission(self):
        zero = Payment.objects.create(booking=make_booking(price='250.00', commission='0.00'))
        self.assertEqual(zero.commission_amount, Decimal('0.00'))
        self.assertEqual(zero.payout_amount, Decimal('250.00'))
        full = Payment.objects.create(booking=make_booking(price='250.00', commission='100.00'))
        self.assertEqual(full.commission_amount, Decimal('250.00'))
        self.assertEqual(full.payout_amount, Decimal('0.00'))

    def test_booking_snapshots_ignore_later_listing_changes(self):
        booking = make_booking(price='500.00', commission='12.50')
        ProviderService.objects.filter(pk=booking.provider_service_id).update(price=Decimal('900.00'))
        Provider.objects.filter(
            pk=booking.provider_service.provider_id
        ).update(commission_rate=Decimal('50.00'))
        booking.save()  # a plain re-save must not re-snapshot
        booking.refresh_from_db()
        self.assertEqual(booking.price_snapshot, Decimal('500.00'))
        self.assertEqual(booking.commission_rate_snapshot, Decimal('12.50'))


class PaymentConstraintTests(FarhetiTestCase):
    def setUp(self):
        super().setUp()
        self.booking = make_booking(status=B.ACCEPTED)
        self.payment = Payment.objects.create(booking=self.booking)

    def rejected(self, **fields):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Payment.objects.filter(pk=self.payment.pk).update(**fields)

    def test_payout_requires_paid(self):
        self.rejected(payout_completed=True)

    def test_refund_requires_paid(self):
        self.rejected(refunded_at=timezone.now())

    def test_payout_blocked_if_refunded(self):
        now = timezone.now()
        Payment.objects.filter(pk=self.payment.pk).update(paid_at=now, refunded_at=now)
        self.rejected(payout_completed=True)

    def test_non_positive_amount_rejected(self):
        self.rejected(amount=Decimal('0.00'))

    def test_one_payment_per_booking(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Payment.objects.create(booking=self.booking)


class RecordRefundedChargeTests(FarhetiTestCase):
    def setUp(self):
        super().setUp()
        self.booking, self.payment = make_paid_booking(txn_id='1001')

    def test_records_the_charge_without_touching_the_real_payment(self):
        before = (self.payment.paid_at, self.payment.refunded_at, self.payment.paymob_transaction_id)
        self.payment.record_refunded_charge('2002', Decimal('500.00'), 'R-1')
        charge = RefundedCharge.objects.get()
        self.assertEqual((charge.paymob_transaction_id, charge.amount, charge.refund_reference),
                         ('2002', Decimal('500.00'), 'R-1'))
        self.payment.refresh_from_db()
        self.assertEqual(
            (self.payment.paid_at, self.payment.refunded_at, self.payment.paymob_transaction_id), before
        )

    def test_same_transaction_twice_is_a_no_op(self):
        self.payment.record_refunded_charge('2002', Decimal('500.00'), 'R-1')
        self.payment.record_refunded_charge('2002', Decimal('500.00'), 'R-1')
        self.assertEqual(RefundedCharge.objects.count(), 1)

    def test_refund_reference_is_required(self):
        with self.assertRaises(ValidationError):
            self.payment.record_refunded_charge('2002', Decimal('500.00'), '')
        self.assertFalse(RefundedCharge.objects.exists())


class BookingAcceptTests(FarhetiTestCase):
    def test_accept_sets_a_24h_payment_window(self):
        booking = make_booking(days_ahead=10)
        booking.mark_accepted()
        self.assertEqual(booking.status, B.ACCEPTED)
        self.assertEqual(booking.payment_deadline - booking.accepted_at, timedelta(hours=24))

    def test_lead_time_boundary(self):
        for days, ok in ((0, False), (1, False), (MIN_DAYS_AHEAD_TO_ACCEPT, True), (3, True)):
            with self.subTest(days_ahead=days):
                booking = make_booking(days_ahead=days)
                if ok:
                    booking.mark_accepted()
                    self.assertEqual(booking.status, B.ACCEPTED)
                else:
                    with self.assertRaisesMessage(ValidationError, 'too close'):
                        booking.mark_accepted()
                    booking.refresh_from_db()
                    self.assertEqual(booking.status, B.PENDING)

    def test_only_pending_can_be_accepted(self):
        for status in (B.ACCEPTED, B.PAID, B.DECLINED, B.EXPIRED, B.CANCELLED, B.COMPLETED):
            with self.subTest(status=status):
                booking = make_booking(status=status)
                with self.assertRaises(ValidationError):
                    booking.mark_accepted()

    def test_non_bookable_listing_cannot_be_accepted(self):
        for how in BREAK_MODES:
            with self.subTest(how=how):
                listing = make_listing()
                booking = make_booking(listing=listing)
                break_listing(listing, how)
                with self.assertRaisesMessage(ValidationError, 'no longer bookable'):
                    booking.mark_accepted()


class BookingOtherTransitionTests(FarhetiTestCase):
    def test_decline(self):
        booking = make_booking()
        booking.mark_declined()
        self.assertEqual(booking.status, B.DECLINED)
        with self.assertRaises(ValidationError):
            booking.mark_declined()

    def test_cancel_pending_and_accepted_only(self):
        for status, ok in ((B.PENDING, True), (B.ACCEPTED, True), (B.PAID, False),
                           (B.DECLINED, False), (B.EXPIRED, False), (B.COMPLETED, False)):
            with self.subTest(status=status):
                booking = make_booking(status=status)
                if ok:
                    booking.mark_cancelled()
                    self.assertEqual(booking.status, B.CANCELLED)
                else:
                    with self.assertRaises(ValidationError):
                        booking.mark_cancelled()

    def test_paid_booking_cancel_message_points_to_the_refund_flow(self):
        booking, _ = make_paid_booking()
        with self.assertRaisesMessage(ValidationError, 'mark_cancelled_and_refunded'):
            booking.mark_cancelled()

    def test_complete_needs_paid_and_event_date_reached(self):
        booking, _ = make_paid_booking(days_ahead=0)
        booking.mark_completed()
        self.assertEqual(booking.status, B.COMPLETED)
        future, _ = make_paid_booking(txn_id='1002', days_ahead=1)
        with self.assertRaisesMessage(ValidationError, 'before its event date'):
            future.mark_completed()
        not_paid = make_booking(status=B.ACCEPTED, days_ahead=-1)
        with self.assertRaises(ValidationError):
            not_paid.mark_completed()

    def test_complete_is_blocked_while_a_cancellation_refund_is_open(self):
        for status in (S.IN_FLIGHT, S.UNKNOWN, S.GATEWAY_REFUNDED):
            with self.subTest(status=status):
                booking, payment = make_paid_booking(txn_id=f'30{status}', days_ahead=-1)
                make_refund_attempt(payment, status)
                with self.assertRaisesMessage(ValidationError, 'refund'):
                    booking.mark_completed()
                booking.refresh_from_db()
                self.assertEqual(booking.status, B.PAID)

    def test_complete_is_allowed_after_a_failed_refund_or_for_other_kinds(self):
        booking, payment = make_paid_booking(txn_id='3101', days_ahead=-1)
        make_refund_attempt(payment, S.FAILED)
        booking.mark_completed()
        other, other_payment = make_paid_booking(txn_id='3102', days_ahead=-1)
        make_refund_attempt(other_payment, S.IN_FLIGHT, txn='4102', kind=K.UNCONFIRMED)
        other.mark_completed()

    def test_expire_needs_accepted_and_a_passed_deadline(self):
        booking = make_booking(status=B.ACCEPTED)
        with self.assertRaisesMessage(ValidationError, "hasn't passed"):
            booking.mark_expired()
        Booking.objects.filter(pk=booking.pk).update(payment_deadline=timezone.now() - timedelta(seconds=1))
        booking.mark_expired()
        self.assertEqual(booking.status, B.EXPIRED)
        with self.assertRaises(ValidationError):
            make_booking(status=B.PENDING).mark_expired()

    def test_accepted_booking_with_null_deadline_is_never_expired(self):
        booking = make_booking(status=B.ACCEPTED, deadline_in=None)
        self.assertFalse(booking.is_payment_expired)
        with self.assertRaises(ValidationError):
            booking.mark_expired()

    def test_expire_unanswered_rules(self):
        now = timezone.now()
        older_than = now - timedelta(hours=72)
        event_before = timezone.localdate() + timedelta(days=MIN_DAYS_AHEAD_TO_ACCEPT)

        fresh = make_booking(days_ahead=10)
        with self.assertRaisesMessage(ValidationError, 'not stale'):
            fresh.mark_expired_unanswered(older_than=older_than, event_before=event_before)

        old = make_booking(days_ahead=10)
        Booking.objects.filter(pk=old.pk).update(created_at=now - timedelta(hours=73))
        old.mark_expired_unanswered(older_than=older_than, event_before=event_before)
        self.assertEqual(old.status, B.EXPIRED)

        close = make_booking(days_ahead=1)
        close.mark_expired_unanswered(older_than=older_than, event_before=event_before)
        self.assertEqual(close.status, B.EXPIRED)

        with self.assertRaises(ValidationError):
            make_booking(status=B.ACCEPTED).mark_expired_unanswered(
                older_than=older_than, event_before=event_before
            )


class CancelAndRefundModelTests(FarhetiTestCase):
    def test_success_records_the_refund_and_cancels(self):
        booking, payment = make_paid_booking()
        booking.mark_cancelled_and_refunded('R-1')
        payment.refresh_from_db()
        self.assertEqual(booking.status, B.CANCELLED)
        self.assertEqual(payment.refund_reference, 'R-1')
        self.assertIsNotNone(payment.refunded_at)

    def test_reference_is_required(self):
        booking, _ = make_paid_booking()
        with self.assertRaises(ValidationError):
            booking.mark_cancelled_and_refunded('')

    def test_needs_a_paid_booking(self):
        booking = make_booking(status=B.ACCEPTED)
        with self.assertRaises(ValidationError):
            booking.mark_cancelled_and_refunded('R-1')

    def test_refuses_after_payout(self):
        booking, payment = make_paid_booking()
        Payment.objects.filter(pk=payment.pk).update(payout_completed=True)
        with self.assertRaisesMessage(ValidationError, 'payout has already been made'):
            booking.mark_cancelled_and_refunded('R-1')

    def test_refuses_a_second_refund(self):
        booking, payment = make_paid_booking()
        Payment.objects.filter(pk=payment.pk).update(refunded_at=timezone.now())
        with self.assertRaisesMessage(ValidationError, 'already been refunded'):
            booking.mark_cancelled_and_refunded('R-2')
