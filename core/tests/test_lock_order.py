"""
Deterministic lock-order tests.

The rule (see the Booking docstring): Provider -> ProviderService -> Booking -> Payment -> RefundAttempt.
Every select_for_update() call is recorded in order and must never go "backwards" inside one
call of a money function. A wrong order would cause database deadlocks under real traffic.

Only operations whose locks all happen in a single transaction (or in a strictly forward
sequence of them) are checked; each test also requires the expected tables to appear, so a
test can never pass vacuously.
"""
from decimal import Decimal
from unittest import mock

from django.urls import reverse

from core import payments
from core.models import Booking, Payment

from .helpers import (
    LOCK_RANK, B, K, S, FarhetiTestCase, api, days_from_today, make_booking,
    make_intention, make_listing, make_paid_booking, make_refund_attempt, make_user,
    record_locks, refund_failed,
)


class LockOrderTests(FarhetiTestCase):
    def assert_lock_order(self, func, expect, raises=None):
        with record_locks() as order:
            if raises is None:
                func()
            else:
                with self.assertRaises(raises):
                    func()
        self.assertTrue(set(expect) <= set(order), f'expected locks on {expect}, saw {order}')
        ranks = [LOCK_RANK[name] for name in order]
        self.assertEqual(ranks, sorted(ranks), f'locks taken out of order: {order}')

    # ---- model methods
    def test_mark_paid_locks_booking_before_payment(self):
        booking = make_booking(status=B.ACCEPTED)
        payment = Payment.objects.create(booking=booking)
        self.assert_lock_order(
            lambda: payment.mark_paid_and_confirm_booking('1001', Decimal('1000.00')),
            ['booking', 'payment'],
        )

    def test_record_refunded_charge_locks_booking_before_payment(self):
        _, payment = make_paid_booking()
        self.assert_lock_order(
            lambda: payment.record_refunded_charge('2002', Decimal('500.00'), 'R-1'),
            ['booking', 'payment'],
        )

    def test_cancel_and_refund_model_method_locks_booking_before_payment(self):
        booking, _ = make_paid_booking()
        self.assert_lock_order(lambda: booking.mark_cancelled_and_refunded('R-1'), ['booking', 'payment'])

    # ---- payments.py flows
    def test_start_checkout_follows_the_full_chain(self):
        booking = make_booking(status=B.ACCEPTED)
        with mock.patch('core.paymob.create_intention', return_value=make_intention()):
            self.assert_lock_order(
                lambda: payments.start_checkout(booking_id=booking.pk, user=booking.customer),
                ['provider', 'providerservice', 'booking', 'payment'],
            )

    def test_mark_payout_done_follows_the_full_chain(self):
        _, payment = make_paid_booking(days_ahead=-1)
        self.assert_lock_order(
            lambda: payments.mark_payout_done(payment_id=payment.pk, reference='R', actor=None),
            ['provider', 'providerservice', 'booking', 'payment'],
        )

    def test_cancel_and_refund_follows_the_full_chain_including_the_attempt(self):
        booking, _ = make_paid_booking()
        with mock.patch('core.paymob.refund_transaction', return_value=refund_failed(definitive=True)):
            self.assert_lock_order(
                lambda: payments.cancel_and_refund(
                    booking_id=booking.pk, actor=booking.customer, role='customer'
                ),
                ['provider', 'providerservice', 'booking', 'payment', 'refundattempt'],
                raises=payments.FlowRefundIncomplete,
            )

    def test_refund_unconfirmed_charge_locks_booking_payment_attempt(self):
        _, payment = make_paid_booking()
        with mock.patch('core.paymob.refund_transaction', return_value=refund_failed(definitive=True)):
            self.assert_lock_order(
                lambda: payments.refund_unconfirmed_charge(
                    payment_id=payment.pk, txn_id='2002', amount=Decimal('500.00'), reason='t'
                ),
                ['booking', 'payment', 'refundattempt'],
            )

    def test_retry_refund_attempt_locks_booking_payment_attempt(self):
        _, payment = make_paid_booking()
        attempt = make_refund_attempt(payment, S.FAILED, txn='2002', kind=K.UNCONFIRMED,
                                      amount=Decimal('500.00'), attempts=1)
        with mock.patch('core.paymob.refund_transaction', return_value=refund_failed(definitive=True)):
            self.assert_lock_order(
                lambda: payments.retry_refund_attempt(attempt_id=attempt.pk),
                ['booking', 'payment', 'refundattempt'],
            )

    def test_mark_attempt_not_refunded_locks_booking_payment_attempt(self):
        _, payment = make_paid_booking()
        attempt = make_refund_attempt(payment, S.UNKNOWN)
        self.assert_lock_order(
            lambda: payments.mark_attempt_not_refunded(attempt_id=attempt.pk, actor=None),
            ['booking', 'payment', 'refundattempt'],
        )

    # ---- API flows
    def test_booking_accept_locks_provider_listing_booking(self):
        listing = make_listing()
        booking = make_booking(listing=listing, days_ahead=10)
        client = api(listing.provider.user)
        self.assert_lock_order(
            lambda: client.post(reverse('booking-accept', args=[booking.pk]), {}, format='json'),
            ['provider', 'providerservice', 'booking'],
        )
        booking.refresh_from_db()
        self.assertEqual(booking.status, B.ACCEPTED)

    def test_booking_create_locks_provider_then_listing(self):
        listing = make_listing()
        client = api(make_user())
        payload = {'provider_service': listing.pk, 'event_date': days_from_today(10).isoformat()}
        self.assert_lock_order(
            lambda: client.post(reverse('booking-list'), payload, format='json'),
            ['provider', 'providerservice'],
        )
        self.assertEqual(Booking.objects.count(), 1)

    def test_the_spy_itself_detects_a_wrong_order(self):
        # Guards against the helper silently passing everything.
        booking, payment = make_paid_booking()

        def backwards():
            Payment.objects.select_for_update().get(pk=payment.pk)
            Booking.objects.select_for_update().get(pk=booking.pk)

        with self.assertRaises(AssertionError):
            self.assert_lock_order(backwards, ['booking', 'payment'])
