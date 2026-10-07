from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.admin.models import LogEntry
from django.utils import timezone

from core import payments
from core.models import Booking, Payment, RefundAttempt, RefundedCharge

from .helpers import (
    B, K, S, FarhetiTestCase, make_booking, make_paid_booking, make_refund_attempt,
    make_user, refund_failed, refund_ok,
)


class RefundTestBase(FarhetiTestCase):
    def setUp(self):
        super().setUp()
        self.booking, self.payment = make_paid_booking(txn_id='1001', days_ahead=10)
        self.customer = self.booking.customer
        self.provider_user = self.booking.provider_service.provider.user
        patcher = mock.patch('core.paymob.refund_transaction', return_value=refund_ok('9001'))
        self.gateway = patcher.start()
        self.addCleanup(patcher.stop)

    def reload(self):
        self.booking.refresh_from_db()
        self.payment.refresh_from_db()

    def set_event_in(self, days):
        Booking.objects.filter(pk=self.booking.pk).update(
            event_date=timezone.localdate() + timedelta(days=days)
        )


class CancelAndRefundTests(RefundTestBase):
    def cancel(self, role='customer'):
        actor = {'customer': self.customer, 'provider': self.provider_user}.get(role)
        if actor is None:
            actor = make_user(is_staff=True)
        return payments.cancel_and_refund(booking_id=self.booking.pk, actor=actor, role=role)

    def test_customer_gets_a_full_refund_and_the_booking_is_cancelled(self):
        attempt = self.cancel('customer')
        self.assertEqual(attempt.status, S.DONE)
        self.gateway.assert_called_once_with(transaction_id=1001, amount_cents=100000)
        self.reload()
        self.assertEqual(self.booking.status, B.CANCELLED)
        self.assertIsNotNone(self.payment.refunded_at)
        self.assertEqual(self.payment.refund_reference, '9001')
        self.assertEqual(attempt.requested_by_id, self.customer.pk)
        self.assertEqual(LogEntry.objects.count(), 0)  # only staff actions are audited

    def test_staff_actions_are_written_to_the_audit_log(self):
        staff = make_user(is_staff=True)
        payments.cancel_and_refund(booking_id=self.booking.pk, actor=staff, role='staff')
        self.assertEqual(LogEntry.objects.filter(user=staff).count(), 2)  # started + completed

    def test_customer_needs_seven_days_notice(self):
        for days, allowed in ((6, False), (7, True)):
            with self.subTest(days=days):
                booking, _ = make_paid_booking(txn_id=f'11{days}', days_ahead=days)
                gateway_calls = self.gateway.call_count
                call = lambda: payments.cancel_and_refund(  # noqa: E731
                    booking_id=booking.pk, actor=booking.customer, role='customer'
                )
                if allowed:
                    self.assertEqual(call().status, S.DONE)
                else:
                    with self.assertRaisesMessage(payments.FlowForbidden, '7 or more days'):
                        call()
                    self.assertEqual(self.gateway.call_count, gateway_calls)

    def test_provider_can_cancel_only_before_the_event_day(self):
        for days, allowed in ((0, False), (-1, False), (1, True)):
            with self.subTest(days=days):
                booking, _ = make_paid_booking(txn_id=f'12{days}', days_ahead=days)
                provider_user = booking.provider_service.provider.user
                call = lambda: payments.cancel_and_refund(  # noqa: E731
                    booking_id=booking.pk, actor=provider_user, role='provider'
                )
                if allowed:
                    self.assertEqual(call().status, S.DONE)
                else:
                    with self.assertRaises(payments.FlowForbidden):
                        call()

    def test_staff_is_not_bound_by_the_date_policy(self):
        self.set_event_in(-3)
        self.assertEqual(self.cancel('staff').status, S.DONE)

    def test_only_a_paid_booking_with_a_payment_can_be_refunded(self):
        no_payment = make_booking(status=B.ACCEPTED)
        with self.assertRaisesMessage(payments.FlowConflict, 'no payment'):
            payments.cancel_and_refund(booking_id=no_payment.pk, actor=no_payment.customer, role='customer')
        unpaid = make_booking(status=B.ACCEPTED)
        Payment.objects.create(booking=unpaid)
        with self.assertRaisesMessage(payments.FlowConflict, 'Only paid bookings'):
            payments.cancel_and_refund(booking_id=unpaid.pk, actor=unpaid.customer, role='customer')
        self.gateway.assert_not_called()

    def test_refused_after_payout_or_when_already_refunded_or_without_a_transaction(self):
        cases = (
            ('payout', dict(payout_completed=True), 'payout'),
            ('refunded', dict(refunded_at=timezone.now()), 'already been refunded'),
            ('no txn', dict(paymob_transaction_id=None), 'automatically'),
        )
        for index, (name, fields, message) in enumerate(cases):
            with self.subTest(case=name):
                booking, payment = make_paid_booking(txn_id=f'130{index}')
                Payment.objects.filter(pk=payment.pk).update(**fields)
                with self.assertRaisesMessage(payments.FlowConflict, message):
                    payments.cancel_and_refund(booking_id=booking.pk, actor=booking.customer, role='customer')
        self.gateway.assert_not_called()

    def test_double_click_refunds_once(self):
        self.cancel('customer')
        with self.assertRaisesMessage(payments.FlowConflict, 'Only paid bookings'):
            self.cancel('customer')
        self.assertEqual(self.gateway.call_count, 1)
        self.assertEqual(RefundAttempt.objects.count(), 1)

    def test_an_attempt_that_is_being_processed_blocks_a_second_one(self):
        attempt = make_refund_attempt(self.payment, S.IN_FLIGHT, started_ago=timedelta(seconds=5))
        with self.assertRaisesMessage(payments.FlowConflict, 'already being processed'):
            self.cancel('customer')
        self.gateway.assert_not_called()
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, S.IN_FLIGHT)

    def test_an_existing_done_attempt_blocks(self):
        make_refund_attempt(self.payment, S.DONE)
        with self.assertRaisesMessage(payments.FlowConflict, 'already been refunded'):
            self.cancel('customer')
        self.gateway.assert_not_called()

    def test_definitive_failure_leaves_the_booking_paid(self):
        self.gateway.return_value = refund_failed(definitive=True, error='declined')
        with self.assertRaises(payments.FlowRefundIncomplete):
            self.cancel('customer')
        self.reload()
        self.assertEqual(self.booking.status, B.PAID)
        self.assertIsNone(self.payment.refunded_at)
        attempt = RefundAttempt.objects.get()
        self.assertEqual(attempt.status, S.FAILED)
        self.assertIn('declined', attempt.last_error)

    def test_after_a_failure_only_staff_may_try_again(self):
        self.gateway.return_value = refund_failed(definitive=True)
        with self.assertRaises(payments.FlowRefundIncomplete):
            self.cancel('customer')
        with self.assertRaisesMessage(payments.FlowConflict, 'contact support'):
            self.cancel('customer')
        self.assertEqual(self.gateway.call_count, 1)
        self.gateway.return_value = refund_ok('9002')
        attempt = self.cancel('staff')
        self.assertEqual(attempt.status, S.DONE)
        self.assertEqual(attempt.attempts, 2)
        self.reload()
        self.assertEqual(self.booking.status, B.CANCELLED)
        self.assertEqual(self.payment.refund_reference, '9002')

    def test_unknown_outcome_is_parked_and_never_resent(self):
        self.gateway.return_value = refund_failed(definitive=False, error='network error: Timeout')
        with self.assertRaises(payments.FlowRefundIncomplete):
            self.cancel('customer')
        self.assertEqual(RefundAttempt.objects.get().status, S.UNKNOWN)
        with self.assertRaisesMessage(payments.FlowConflict, 'support'):
            self.cancel('customer')
        with self.assertRaisesMessage(payments.FlowConflict, 'Paymob dashboard'):
            self.cancel('staff')
        self.assertEqual(self.gateway.call_count, 1)
        self.reload()
        self.assertEqual(self.booking.status, B.PAID)

    def test_abandoned_in_flight_attempt_is_parked_and_the_park_is_kept(self):
        attempt = make_refund_attempt(self.payment, S.IN_FLIGHT, started_ago=timedelta(minutes=10))
        with self.assertRaises(payments.FlowConflict):
            self.cancel('customer')
        attempt.refresh_from_db()
        # the FlowConflict is raised outside the transaction, so the UNKNOWN status survives it
        self.assertEqual(attempt.status, S.UNKNOWN)
        self.assertIn('abandoned', attempt.last_error)
        self.gateway.assert_not_called()

    def test_gateway_refunded_attempt_is_finished_without_calling_paymob_again(self):
        self.set_event_in(3)  # customer would normally be too late...
        make_refund_attempt(self.payment, S.GATEWAY_REFUNDED, refund_reference='9001', attempts=1)
        attempt = self.cancel('customer')  # ...but the money already left, so only the DB is finished
        self.assertEqual(attempt.status, S.DONE)
        self.gateway.assert_not_called()
        self.reload()
        self.assertEqual(self.booking.status, B.CANCELLED)
        self.assertEqual(self.payment.refund_reference, '9001')


class RetryRefundAttemptTests(RefundTestBase):
    def unconfirmed(self, status, **fields):
        return make_refund_attempt(
            self.payment, status, txn='2002', kind=K.UNCONFIRMED, amount=Decimal('500.00'), **fields
        )

    def test_retrying_a_failed_unconfirmed_refund_records_the_charge(self):
        attempt = self.unconfirmed(S.FAILED, attempts=1)
        result = payments.retry_refund_attempt(attempt_id=attempt.pk)
        self.assertEqual(result.status, S.DONE)
        self.gateway.assert_called_once_with(transaction_id=2002, amount_cents=50000)
        self.assertEqual(RefundedCharge.objects.get().paymob_transaction_id, '2002')
        self.reload()
        self.assertEqual(self.booking.status, B.PAID)  # the real payment is untouched

    def test_unknown_in_flight_and_done_cannot_be_retried(self):
        done = self.unconfirmed(S.DONE)
        with self.assertRaisesMessage(payments.FlowConflict, 'already completed'):
            payments.retry_refund_attempt(attempt_id=done.pk)
        busy = make_refund_attempt(self.payment, S.IN_FLIGHT, txn='2003', kind=K.UNCONFIRMED,
                                   started_ago=timedelta(seconds=3))
        with self.assertRaisesMessage(payments.FlowConflict, 'currently being processed'):
            payments.retry_refund_attempt(attempt_id=busy.pk)
        unknown = make_refund_attempt(self.payment, S.UNKNOWN, txn='2004', kind=K.UNCONFIRMED)
        with self.assertRaisesMessage(payments.FlowConflict, 'Outcome unknown'):
            payments.retry_refund_attempt(attempt_id=unknown.pk)
        self.gateway.assert_not_called()

    def test_stale_in_flight_is_parked_by_a_retry_attempt(self):
        stale = make_refund_attempt(self.payment, S.IN_FLIGHT, txn='2005', kind=K.UNCONFIRMED)
        with self.assertRaisesMessage(payments.FlowConflict, 'Outcome unknown'):
            payments.retry_refund_attempt(attempt_id=stale.pk)
        stale.refresh_from_db()
        self.assertEqual(stale.status, S.UNKNOWN)
        self.gateway.assert_not_called()

    def test_missing_attempt(self):
        with self.assertRaises(payments.FlowNotFound):
            payments.retry_refund_attempt(attempt_id=99999999)

    def test_cancellation_retry_needs_the_booking_to_be_still_cancellable(self):
        attempt = make_refund_attempt(self.payment, S.FAILED, attempts=1)
        Booking.objects.filter(pk=self.booking.pk).update(status=B.CANCELLED)
        with self.assertRaisesMessage(payments.FlowConflict, 'can no longer be cancelled'):
            payments.retry_refund_attempt(attempt_id=attempt.pk)
        Booking.objects.filter(pk=self.booking.pk).update(status=B.PAID)
        Payment.objects.filter(pk=self.payment.pk).update(payout_completed=True)
        with self.assertRaisesMessage(payments.FlowConflict, 'can no longer be cancelled'):
            payments.retry_refund_attempt(attempt_id=attempt.pk)
        self.gateway.assert_not_called()

    def test_gateway_refunded_but_booking_cannot_be_finalised_is_flagged_for_reconcile(self):
        attempt = make_refund_attempt(self.payment, S.GATEWAY_REFUNDED, refund_reference='9001', attempts=1)
        Booking.objects.filter(pk=self.booking.pk).update(status=B.COMPLETED)
        with self.assertLogs('farheti.payments', 'CRITICAL'):
            with self.assertRaisesMessage(payments.FlowConflict, 'could not be finalized'):
                payments.retry_refund_attempt(attempt_id=attempt.pk)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, S.GATEWAY_REFUNDED)
        self.assertTrue(attempt.last_error.startswith('RECONCILE'))
        self.gateway.assert_not_called()
        self.reload()
        self.assertIsNone(self.payment.refunded_at)

    def test_staff_retry_is_audited(self):
        staff = make_user(is_staff=True)
        attempt = self.unconfirmed(S.FAILED, attempts=1)
        payments.retry_refund_attempt(attempt_id=attempt.pk, actor=staff)
        self.assertEqual(LogEntry.objects.filter(user=staff).count(), 1)


class ResolveManuallyTests(RefundTestBase):
    def test_owner_refunded_by_hand_so_only_the_database_is_finished(self):
        attempt = make_refund_attempt(self.payment, S.UNKNOWN)
        staff = make_user(is_staff=True)
        result = payments.resolve_attempt_manually(
            attempt_id=attempt.pk, reference='  BANK-REF-1  ', actor=staff
        )
        self.assertEqual(result.status, S.DONE)
        self.assertEqual(result.refund_reference, 'BANK-REF-1')  # stripped
        self.gateway.assert_not_called()
        self.reload()
        self.assertEqual(self.booking.status, B.CANCELLED)
        self.assertEqual(self.payment.refund_reference, 'BANK-REF-1')
        self.assertEqual(LogEntry.objects.filter(user=staff).count(), 1)

    def test_unconfirmed_attempt_is_recorded_as_a_refunded_charge(self):
        attempt = make_refund_attempt(self.payment, S.UNKNOWN, txn='2002', kind=K.UNCONFIRMED,
                                      amount=Decimal('500.00'))
        payments.resolve_attempt_manually(attempt_id=attempt.pk, reference='BANK-2', actor=None)
        charge = RefundedCharge.objects.get()
        self.assertEqual((charge.paymob_transaction_id, charge.refund_reference), ('2002', 'BANK-2'))

    def test_reference_rules(self):
        attempt = make_refund_attempt(self.payment, S.UNKNOWN)
        for bad in ('', '   ', None, 'x' * 151):
            with self.subTest(reference=bad):
                with self.assertRaisesMessage(payments.FlowConflict, 'reference'):
                    payments.resolve_attempt_manually(attempt_id=attempt.pk, reference=bad, actor=None)

    def test_done_busy_and_missing(self):
        done = make_refund_attempt(self.payment, S.DONE)
        with self.assertRaisesMessage(payments.FlowConflict, 'already completed'):
            payments.resolve_attempt_manually(attempt_id=done.pk, reference='R', actor=None)
        busy = make_refund_attempt(self.payment, S.IN_FLIGHT, txn='2003', kind=K.UNCONFIRMED,
                                   started_ago=timedelta(seconds=3))
        with self.assertRaisesMessage(payments.FlowConflict, 'currently being processed'):
            payments.resolve_attempt_manually(attempt_id=busy.pk, reference='R', actor=None)
        with self.assertRaises(payments.FlowNotFound):
            payments.resolve_attempt_manually(attempt_id=99999999, reference='R', actor=None)


class MarkNotRefundedTests(RefundTestBase):
    def test_unknown_and_failed_are_cleared_to_a_clean_failed(self):
        for status in (S.UNKNOWN, S.FAILED):
            with self.subTest(status=status):
                payment = make_paid_booking(txn_id=f'14{status}')[1]
                attempt = make_refund_attempt(payment, status, attempts=3)
                result = payments.mark_attempt_not_refunded(attempt_id=attempt.pk, actor=None)
                self.assertEqual(result.status, S.FAILED)
                self.assertEqual(result.attempts, 0)
                self.assertTrue(result.last_error.startswith(payments.CLEARED_NOT_REFUNDED_PREFIX))

    def test_other_states_cannot_be_cleared(self):
        for status in (S.IN_FLIGHT, S.GATEWAY_REFUNDED, S.DONE):
            with self.subTest(status=status):
                payment = make_paid_booking(txn_id=f'15{status}')[1]
                attempt = make_refund_attempt(payment, status)
                with self.assertRaisesMessage(payments.FlowConflict, 'Only unknown or failed'):
                    payments.mark_attempt_not_refunded(attempt_id=attempt.pk, actor=None)

    def test_missing_attempt(self):
        with self.assertRaises(payments.FlowNotFound):
            payments.mark_attempt_not_refunded(attempt_id=99999999, actor=None)

    def test_documented_recovery_path_clear_then_retry(self):
        attempt = make_refund_attempt(self.payment, S.UNKNOWN, attempts=2)
        payments.mark_attempt_not_refunded(attempt_id=attempt.pk, actor=None)
        result = payments.retry_refund_attempt(attempt_id=attempt.pk)
        self.assertEqual(result.status, S.DONE)
        self.reload()
        self.assertEqual(self.booking.status, B.CANCELLED)

    def test_after_clearing_a_failed_retry_is_trusted_as_definitive_again(self):
        attempt = make_refund_attempt(self.payment, S.UNKNOWN, attempts=2)
        payments.mark_attempt_not_refunded(attempt_id=attempt.pk, actor=None)
        self.gateway.return_value = refund_failed(definitive=True)
        result = payments.retry_refund_attempt(attempt_id=attempt.pk)
        self.assertEqual(result.status, S.FAILED)  # attempts was reset, so first try again


class RefundAttemptStateMachineTests(RefundTestBase):
    def refund_unconfirmed(self, txn='2002', amount='500.00'):
        return payments.refund_unconfirmed_charge(
            payment_id=self.payment.pk, txn_id=txn, amount=Decimal(amount), reason='test'
        )

    def test_never_refunds_the_legitimate_charge_through_the_unconfirmed_path(self):
        self.assertIsNone(self.refund_unconfirmed(txn='1001', amount='1000.00'))
        self.gateway.assert_not_called()
        self.assertFalse(RefundAttempt.objects.exists())

    def test_first_definitive_failure_is_failed_then_unknown(self):
        self.gateway.return_value = refund_failed(definitive=True)
        self.assertEqual(self.refund_unconfirmed(), S.FAILED)
        self.assertEqual(self.refund_unconfirmed(), S.UNKNOWN)
        self.assertEqual(self.refund_unconfirmed(), S.UNKNOWN)  # parked: no more gateway calls
        self.assertEqual(self.gateway.call_count, 2)

    def test_a_done_attempt_is_returned_without_calling_the_gateway(self):
        self.assertEqual(self.refund_unconfirmed(), S.DONE)
        self.assertEqual(self.refund_unconfirmed(), S.DONE)
        self.assertEqual(self.gateway.call_count, 1)

    def test_non_numeric_transaction_id_fails_cleanly(self):
        self.assertEqual(self.refund_unconfirmed(txn='abc'), S.FAILED)
        self.assertIn('non-numeric', RefundAttempt.objects.get().last_error)
        self.gateway.assert_not_called()

    def test_a_lost_claim_stops_before_finalising(self):
        def another_worker_takes_over(**kwargs):
            RefundAttempt.objects.filter(paymob_transaction_id='2002').update(
                started_at=timezone.now() + timedelta(seconds=5)
            )
            return refund_ok('9001')

        self.gateway.side_effect = another_worker_takes_over
        with self.assertLogs('farheti.payments', 'CRITICAL'):
            status = self.refund_unconfirmed()
        self.assertEqual(status, S.IN_FLIGHT)  # the status write was dropped
        self.assertFalse(RefundedCharge.objects.exists())

    def test_set_attempt_with_a_stale_token_writes_nothing(self):
        attempt = make_refund_attempt(self.payment, S.IN_FLIGHT, started_ago=timedelta(seconds=5))
        with self.assertLogs('farheti.payments', 'CRITICAL'):
            written = payments._set_attempt(
                attempt.pk, token=attempt.started_at - timedelta(seconds=1), status=S.DONE
            )
        self.assertFalse(written)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, S.IN_FLIGHT)

    def test_set_attempt_truncates_last_error(self):
        attempt = make_refund_attempt(self.payment, S.FAILED)
        payments._set_attempt(attempt.pk, last_error='x' * 400)
        attempt.refresh_from_db()
        self.assertEqual(len(attempt.last_error), 255)

    def test_freshness_boundary(self):
        edge = payments.IN_FLIGHT_STALE_SECONDS
        fresh = RefundAttempt(status=S.IN_FLIGHT, started_at=timezone.now() - timedelta(seconds=edge - 5))
        stale = RefundAttempt(status=S.IN_FLIGHT, started_at=timezone.now() - timedelta(seconds=edge + 5))
        never = RefundAttempt(status=S.IN_FLIGHT, started_at=None)
        self.assertTrue(payments._fresh(fresh))
        self.assertFalse(payments._fresh(stale))
        self.assertFalse(payments._fresh(never))
        self.assertTrue(payments._busy(fresh))
        self.assertFalse(payments._busy(stale))
        self.assertFalse(payments._busy(RefundAttempt(status=S.UNKNOWN, started_at=timezone.now())))

    def test_park_if_unresolved(self):
        stale = make_refund_attempt(self.payment, S.IN_FLIGHT, txn='3001')
        fresh = make_refund_attempt(self.payment, S.IN_FLIGHT, txn='3002', started_ago=timedelta(seconds=3))
        no_claim = make_refund_attempt(self.payment, S.IN_FLIGHT, txn='3003', started_ago=None)
        failed = make_refund_attempt(self.payment, S.FAILED, txn='3004')
        unknown = make_refund_attempt(self.payment, S.UNKNOWN, txn='3005')
        with self.assertLogs('farheti.payments', 'ERROR'):
            self.assertTrue(payments._park_if_unresolved(stale))
        self.assertEqual(RefundAttempt.objects.get(pk=stale.pk).status, S.UNKNOWN)
        self.assertFalse(payments._park_if_unresolved(fresh))
        self.assertFalse(payments._park_if_unresolved(no_claim))
        self.assertFalse(payments._park_if_unresolved(failed))
        self.assertTrue(payments._park_if_unresolved(unknown))
        self.assertEqual(RefundAttempt.objects.get(pk=fresh.pk).status, S.IN_FLIGHT)
        self.assertEqual(RefundAttempt.objects.get(pk=failed.pk).status, S.FAILED)
