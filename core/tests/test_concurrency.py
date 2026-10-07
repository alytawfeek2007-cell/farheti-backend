"""
Real concurrency tests: real threads, real PostgreSQL row locks (TransactionTestCase, so data is
actually committed and visible to the other connections). Each worker thread has its own DB
connection and closes it when done.
"""
import itertools
import threading
import time
from decimal import Decimal
from unittest import mock

from django.core.exceptions import ValidationError
from django.urls import reverse

from core import payments
from core.models import Booking, Payment, PaymentAttempt, RefundAttempt, RefundedCharge

from .helpers import (
    B, K, FarhetiTransactionTestCase, api, days_from_today, make_booking, make_intention,
    make_listing, make_paid_booking, make_user, refund_ok, run_concurrently, values,
)


class BookingRaceTests(FarhetiTransactionTestCase):
    def test_two_accepts_for_the_same_provider_and_date_only_one_wins(self):
        listing = make_listing()
        first = make_booking(listing=listing, customer=make_user(), days_ahead=10)
        second = make_booking(listing=listing, customer=make_user(), days_ahead=10)
        provider_user = listing.provider.user

        def accept(booking):
            return lambda: api(provider_user).post(
                reverse('booking-accept', args=[booking.pk]), {}, format='json'
            ).status_code

        codes = sorted(values(run_concurrently([accept(first), accept(second)])))
        self.assertEqual(codes, [200, 409])
        self.assertEqual(Booking.objects.filter(status=B.ACCEPTED).count(), 1)
        self.assertEqual(Booking.objects.filter(status=B.PENDING).count(), 1)

    def test_two_identical_create_requests_make_one_booking(self):
        listing = make_listing()
        customer = make_user()
        payload = {'provider_service': listing.pk, 'event_date': days_from_today(10).isoformat()}

        def create():
            return api(customer).post(reverse('booking-list'), payload, format='json').status_code

        codes = sorted(values(run_concurrently([create, create])))
        self.assertEqual(codes, [201, 400])
        self.assertEqual(Booking.objects.count(), 1)


class RefundRaceTests(FarhetiTransactionTestCase):
    def test_double_click_cancel_and_refund_hits_the_gateway_once(self):
        booking, _ = make_paid_booking(days_ahead=10)
        calls = []
        guard = threading.Lock()

        def slow_gateway(**kwargs):
            with guard:
                calls.append(kwargs['transaction_id'])
            time.sleep(0.5)  # long enough for the second click to arrive mid-refund
            return refund_ok('9001')

        def click():
            return payments.cancel_and_refund(booking_id=booking.pk, actor=booking.customer, role='customer')

        with mock.patch('core.paymob.refund_transaction', side_effect=slow_gateway):
            results = run_concurrently([click, click])

        self.assertEqual(calls, [1001])  # exactly one money movement
        self.assertEqual(sorted(kind for kind, _ in results), ['err', 'ok'])
        loser = next(value for kind, value in results if kind == 'err')
        self.assertIsInstance(loser, payments.FlowConflict)
        booking.refresh_from_db()
        self.assertEqual(booking.status, B.CANCELLED)
        self.assertEqual(RefundAttempt.objects.count(), 1)
        self.assertIsNotNone(Payment.objects.get().refunded_at)

    def test_payout_and_refund_racing_exactly_one_wins(self):
        booking, payment = make_paid_booking(days_ahead=-1)

        def refund():
            return payments.cancel_and_refund(booking_id=booking.pk, actor=None, role='staff')

        def payout():
            return payments.mark_payout_done(payment_id=payment.pk, reference='BANK-1', actor=None)

        with mock.patch('core.paymob.refund_transaction', side_effect=lambda **kw: refund_ok('9001')):
            results = run_concurrently([refund, payout])

        self.assertEqual(sorted(kind for kind, _ in results), ['err', 'ok'])
        for kind, value in results:
            if kind == 'err':
                self.assertIsInstance(value, payments.PaymentFlowError)
        payment = Payment.objects.get(pk=payment.pk)
        self.assertFalse(payment.payout_completed and payment.refunded_at is not None)
        self.assertTrue(payment.payout_completed or payment.refunded_at is not None)


class WebhookRaceTests(FarhetiTransactionTestCase):
    def setUp(self):
        super().setUp()
        self.booking = make_booking(status=B.ACCEPTED)
        self.payment = Payment.objects.create(booking=self.booking)

    def deliver(self, txn):
        return lambda: payments.handle_successful_charge(
            payment=Payment.objects.get(pk=self.payment.pk), txn_id=txn, amount_cents=100000, currency='EGP'
        )

    def test_the_same_charge_delivered_twice_at_once_confirms_once_and_refunds_nothing(self):
        with mock.patch('core.paymob.refund_transaction', side_effect=AssertionError('must not refund')) as gateway:
            outcomes = values(run_concurrently([self.deliver(1001), self.deliver(1001)]))
        self.assertEqual(outcomes, ['confirmed', 'confirmed'])
        gateway.assert_not_called()
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, B.PAID)
        self.assertEqual(Payment.objects.get().paymob_transaction_id, '1001')
        self.assertFalse(RefundAttempt.objects.exists())

    def test_two_different_charges_at_once_one_confirms_and_the_other_is_refunded(self):
        calls = []
        with mock.patch('core.paymob.refund_transaction',
                        side_effect=lambda **kw: calls.append(kw['transaction_id']) or refund_ok('9001')):
            outcomes = values(run_concurrently([self.deliver(1001), self.deliver(1002)]))
        self.assertEqual(sorted(outcomes), ['confirmed', 'refund_done'])
        payment = Payment.objects.get()
        winner = payment.paymob_transaction_id
        self.assertIn(winner, ('1001', '1002'))
        loser = '1002' if winner == '1001' else '1001'
        self.assertEqual(calls, [int(loser)])
        self.assertEqual(RefundedCharge.objects.get().paymob_transaction_id, loser)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, B.PAID)
        self.assertIsNone(payment.refunded_at)


class CheckoutRaceTests(FarhetiTransactionTestCase):
    def test_double_click_on_pay_creates_one_paymob_session(self):
        booking = make_booking(status=B.ACCEPTED)
        calls = []

        def slow_intention(**kwargs):
            calls.append(kwargs['special_reference'])
            time.sleep(0.4)
            return make_intention()

        def click():
            try:
                return payments.start_checkout(booking_id=booking.pk, user=booking.customer)
            except payments.FlowConflict as exc:  # "a session is being created" is a valid answer
                return exc

        with mock.patch('core.paymob.create_intention', side_effect=slow_intention):
            results = values(run_concurrently([click, click]))

        self.assertEqual(len(calls), 1)
        self.assertEqual(PaymentAttempt.objects.count(), 1)
        self.assertEqual(Payment.objects.count(), 1)
        self.assertTrue(any(isinstance(r, payments.Checkout) for r in results))


class MixedOperationsStressTests(FarhetiTransactionTestCase):
    """
    Many different money operations hammering the SAME booking at once. A lock-order mistake
    shows up as a Postgres deadlock (OperationalError). This test is probabilistic by nature:
    it can only fail when the bug exists, and a real bug is very likely (not certain) to be hit.
    The deterministic guard for lock order is test_lock_order.py.
    """

    def test_no_deadlock_no_double_refund_and_consistent_end_state(self):
        booking, payment = make_paid_booking(txn_id='1001', days_ahead=-1)
        extra_txn = itertools.count(2000)
        gateway_calls = []

        def gateway(**kwargs):
            gateway_calls.append(kwargs['transaction_id'])
            return refund_ok(f"R{kwargs['transaction_id']}")

        def payout():
            payments.mark_payout_done(payment_id=payment.pk, reference='BANK-1', actor=None)

        def cancel():
            payments.cancel_and_refund(booking_id=booking.pk, actor=None, role='staff')

        def complete():
            Booking.objects.get(pk=booking.pk).mark_completed()

        def unconfirmed():
            payments.refund_unconfirmed_charge(
                payment_id=payment.pk, txn_id=str(next(extra_txn)), amount=Decimal('500.00'), reason='stress'
            )

        def reconfirm():
            Payment.objects.get(pk=payment.pk).mark_paid_and_confirm_booking('1001', Decimal('1000.00'))

        def looped(operation):
            def run():
                unexpected = []
                for _ in range(3):
                    try:
                        operation()
                    except (payments.PaymentFlowError, ValidationError):
                        pass  # losing a race with a clear business error is fine
                    except Exception as exc:  # deadlocks, IntegrityErrors, ... are NOT fine
                        unexpected.append(repr(exc))
                return unexpected
            return run

        operations = [payout, cancel, complete, unconfirmed, reconfirm] * 2
        with mock.patch('core.paymob.refund_transaction', side_effect=gateway):
            results = values(run_concurrently([looped(op) for op in operations], timeout=120))

        self.assertEqual([item for sub in results for item in sub], [])
        self.assertLessEqual(gateway_calls.count(1001), 1, 'the real charge was refunded more than once')
        self.assertEqual(
            len([c for c in gateway_calls if c >= 2000]), len(set(c for c in gateway_calls if c >= 2000))
        )
        payment = Payment.objects.get(pk=payment.pk)
        booking = Booking.objects.get(pk=booking.pk)
        self.assertFalse(payment.payout_completed and payment.refunded_at is not None)
        if payment.refunded_at is not None:
            self.assertEqual(booking.status, B.CANCELLED)
        if payment.payout_completed:
            self.assertIn(booking.status, (B.PAID, B.COMPLETED))
        self.assertLessEqual(
            RefundAttempt.objects.filter(kind=K.CANCELLATION).count(), 1
        )
