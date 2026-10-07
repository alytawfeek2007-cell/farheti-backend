from datetime import timedelta

from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import User
from django.utils import timezone

from core import payments
from core.models import Booking, Payment, Provider

from .helpers import (
    B, K, S, FarhetiTestCase, make_booking, make_paid_booking, make_refund_attempt, make_user,
)


class MarkPayoutDoneTests(FarhetiTestCase):
    def setUp(self):
        super().setUp()
        # PAID, event was yesterday: the normal "ready to pay out" state
        self.booking, self.payment = make_paid_booking(days_ahead=-1)
        self.provider = self.booking.provider_service.provider

    def payout(self, reference='BANK-1', actor=None):
        return payments.mark_payout_done(payment_id=self.payment.pk, reference=reference, actor=actor)

    def test_success_records_the_payout_and_audits_staff(self):
        staff = make_user(is_staff=True)
        payment = self.payout('  BANK-1  ', actor=staff)
        self.assertTrue(payment.payout_completed)
        self.assertEqual(payment.payout_reference, 'BANK-1')
        self.assertIsNotNone(payment.payout_completed_at)
        self.assertEqual(LogEntry.objects.filter(user=staff).count(), 1)

    def test_second_payout_is_rejected(self):
        self.payout()
        with self.assertRaisesMessage(payments.FlowConflict, 'already marked as done'):
            self.payout('BANK-2')
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.payout_reference, 'BANK-1')

    def test_reference_rules(self):
        for bad in ('', '   ', None, 'x' * 151):
            with self.subTest(reference=bad):
                with self.assertRaisesMessage(payments.FlowConflict, 'reference'):
                    self.payout(bad)
        self.assertEqual(self.payout('x' * 150).payout_reference, 'x' * 150)

    def test_unknown_payment(self):
        with self.assertRaises(payments.FlowNotFound):
            payments.mark_payout_done(payment_id=99999999, reference='R', actor=None)

    def test_never_paid(self):
        booking = make_booking(status=B.ACCEPTED, days_ahead=-1)
        payment = Payment.objects.create(booking=booking)
        with self.assertRaisesMessage(payments.FlowConflict, 'never paid'):
            payments.mark_payout_done(payment_id=payment.pk, reference='R', actor=None)

    def test_refunded_payment(self):
        Payment.objects.filter(pk=self.payment.pk).update(refunded_at=timezone.now())
        with self.assertRaisesMessage(payments.FlowConflict, 'was refunded'):
            self.payout()

    def test_suspended_provider_or_disabled_login_puts_payouts_on_hold(self):
        Provider.objects.filter(pk=self.provider.pk).update(is_suspended=True)
        with self.assertRaisesMessage(payments.FlowConflict, 'on hold'):
            self.payout()
        Provider.objects.filter(pk=self.provider.pk).update(is_suspended=False)
        User.objects.filter(pk=self.provider.user_id).update(is_active=False)
        with self.assertRaisesMessage(payments.FlowConflict, 'on hold'):
            self.payout()

    def test_self_paused_or_unverified_provider_is_still_paid(self):
        Provider.objects.filter(pk=self.provider.pk).update(is_active=False, is_verified=False)
        self.assertTrue(self.payout().payout_completed)

    def test_booking_status_must_be_paid_or_completed(self):
        for status in (B.PENDING, B.ACCEPTED, B.DECLINED, B.EXPIRED, B.CANCELLED):
            with self.subTest(status=status):
                Booking.objects.filter(pk=self.booking.pk).update(status=status)
                with self.assertRaisesMessage(payments.FlowConflict, 'payout is not allowed'):
                    self.payout()

    def test_paid_booking_needs_the_event_date_to_have_passed(self):
        for days in (0, 1, 30):
            with self.subTest(days_ahead=days):
                Booking.objects.filter(pk=self.booking.pk).update(
                    event_date=timezone.localdate() + timedelta(days=days)
                )
                with self.assertRaisesMessage(payments.FlowConflict, 'has not passed'):
                    self.payout()
        Booking.objects.filter(pk=self.booking.pk).update(event_date=timezone.localdate() - timedelta(days=1))
        self.assertTrue(self.payout().payout_completed)

    def test_completed_booking_can_be_paid_out(self):
        Booking.objects.filter(pk=self.booking.pk).update(status=B.COMPLETED)
        self.assertTrue(self.payout().payout_completed)

    def test_open_or_finished_cancellation_refund_blocks_the_payout(self):
        for index, status in enumerate((S.IN_FLIGHT, S.UNKNOWN, S.GATEWAY_REFUNDED, S.DONE)):
            with self.subTest(status=status):
                booking, payment = make_paid_booking(txn_id=f'50{index}', days_ahead=-1)
                make_refund_attempt(payment, status)
                with self.assertRaisesMessage(payments.FlowConflict, 'cancellation refund'):
                    payments.mark_payout_done(payment_id=payment.pk, reference='R', actor=None)
                payment.refresh_from_db()
                self.assertFalse(payment.payout_completed)

    def test_failed_cancellation_refund_and_other_kinds_do_not_block(self):
        booking, payment = make_paid_booking(txn_id='5100', days_ahead=-1)
        make_refund_attempt(payment, S.FAILED)
        make_refund_attempt(payment, S.IN_FLIGHT, txn='5101', kind=K.UNCONFIRMED)
        self.assertTrue(payments.mark_payout_done(payment_id=payment.pk, reference='R', actor=None).payout_completed)
