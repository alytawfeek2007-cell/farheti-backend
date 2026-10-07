import itertools
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.utils import timezone

from core import payments, paymob
from core.models import Booking, Payment, PaymentAttempt, ProviderService

from .helpers import (
    B, FarhetiTestCase, break_listing, make_booking, make_intention, make_user,
)


class StartCheckoutTests(FarhetiTestCase):
    def setUp(self):
        super().setUp()
        self.booking = make_booking(status=B.ACCEPTED)
        self.customer = self.booking.customer
        order_ids = itertools.count(5001)
        patcher = mock.patch(
            'core.paymob.create_intention',
            side_effect=lambda **kwargs: make_intention(next(order_ids)),
        )
        self.create = patcher.start()
        self.addCleanup(patcher.stop)

    def start(self, user=None, **kwargs):
        return payments.start_checkout(booking_id=self.booking.pk, user=user or self.customer, **kwargs)

    def age_attempts(self, seconds):
        PaymentAttempt.objects.update(created_at=timezone.now() - timedelta(seconds=seconds))

    # ---- happy path
    def test_success_creates_payment_attempt_and_returns_the_checkout_url(self):
        checkout = self.start()
        self.assertFalse(checkout.reused)
        self.assertIn('clientSecret=cs_1', checkout.url)
        payment = Payment.objects.get(booking=self.booking)
        self.assertEqual(payment.amount, Decimal('1000.00'))
        attempt = PaymentAttempt.objects.get()
        self.assertEqual((attempt.paymob_order_id, attempt.client_secret), (5001, 'cs_1'))
        self.assertTrue(attempt.special_reference.startswith(f'fh-{payment.pk}-'))
        kwargs = self.create.call_args.kwargs
        self.assertEqual(kwargs['amount_cents'], 100000)
        self.assertEqual(kwargs['special_reference'], attempt.special_reference)
        self.assertEqual(kwargs['booking_id'], self.booking.pk)
        self.assertEqual(kwargs['expiration_seconds'], 3600)  # capped at one hour

    def test_amount_comes_from_the_snapshot_not_the_live_price(self):
        ProviderService.objects.filter(pk=self.booking.provider_service_id).update(price=Decimal('5.00'))
        self.start()
        self.assertEqual(self.create.call_args.kwargs['amount_cents'], 100000)

    def test_intention_expiry_follows_a_short_remaining_deadline(self):
        Booking.objects.filter(pk=self.booking.pk).update(payment_deadline=timezone.now() + timedelta(seconds=90))
        self.start()
        self.assertTrue(60 <= self.create.call_args.kwargs['expiration_seconds'] <= 90)

    def test_billing_defaults_and_phone(self):
        self.start()
        billing = self.create.call_args.kwargs['billing']
        self.assertEqual((billing['first_name'], billing['last_name']), ('Farheti', 'Customer'))
        self.assertEqual(billing['email'], self.customer.email)
        self.assertEqual(billing['phone_number'], payments.FALLBACK_PHONE)
        self.assertEqual(billing['country'], 'EG')
        self.age_attempts(3600)
        self.start(phone='+201001234567')
        self.assertEqual(self.create.call_args.kwargs['billing']['phone_number'], '+201001234567')

    # ---- ownership
    def test_only_the_customer_can_pay(self):
        provider_user = self.booking.provider_service.provider.user
        for user in (make_user(), provider_user):
            with self.subTest(user=user.pk):
                with self.assertRaises(payments.FlowNotFound):
                    self.start(user=user)
        self.create.assert_not_called()
        self.assertFalse(Payment.objects.exists())

    def test_unknown_booking(self):
        with self.assertRaises(payments.FlowNotFound):
            payments.start_checkout(booking_id=99999999, user=self.customer)

    # ---- eligibility rules
    def test_booking_must_be_accepted(self):
        for status in (B.PENDING, B.DECLINED, B.EXPIRED, B.CANCELLED, B.PAID, B.COMPLETED):
            with self.subTest(status=status):
                Booking.objects.filter(pk=self.booking.pk).update(status=status)
                with self.assertRaisesMessage(payments.FlowConflict, 'not awaiting payment'):
                    self.start()
        self.create.assert_not_called()

    def test_passed_or_missing_deadline_is_rejected(self):
        Booking.objects.filter(pk=self.booking.pk).update(payment_deadline=timezone.now() - timedelta(seconds=1))
        with self.assertRaisesMessage(payments.FlowConflict, 'deadline'):
            self.start()
        Booking.objects.filter(pk=self.booking.pk).update(payment_deadline=None)
        with self.assertRaisesMessage(payments.FlowConflict, 'deadline'):
            self.start()
        self.create.assert_not_called()

    def test_deadline_about_to_pass_is_rejected(self):
        Booking.objects.filter(pk=self.booking.pk).update(payment_deadline=timezone.now() + timedelta(seconds=30))
        with self.assertRaisesMessage(payments.FlowConflict, 'about to pass'):
            self.start()

    def test_event_date_today_or_past_is_rejected(self):
        for delta in (0, -1):
            with self.subTest(days=delta):
                Booking.objects.filter(pk=self.booking.pk).update(
                    event_date=timezone.localdate() + timedelta(days=delta)
                )
                with self.assertRaisesMessage(payments.FlowConflict, 'event date'):
                    self.start()
        self.create.assert_not_called()

    def test_suspended_provider_or_disabled_login_blocks_payment(self):
        for how in ('suspended', 'login_disabled'):
            with self.subTest(how=how):
                booking = make_booking(status=B.ACCEPTED)
                break_listing(booking.provider_service, how)
                with self.assertRaisesMessage(payments.FlowConflict, 'contact support'):
                    payments.start_checkout(booking_id=booking.pk, user=booking.customer)

    def test_self_paused_or_unverified_provider_does_not_block_payment(self):
        for how in ('paused', 'unverified'):
            with self.subTest(how=how):
                booking = make_booking(status=B.ACCEPTED)
                break_listing(booking.provider_service, how)
                checkout = payments.start_checkout(booking_id=booking.pk, user=booking.customer)
                self.assertIn('clientSecret=', checkout.url)

    def test_already_paid_or_refunded_is_rejected(self):
        payment = Payment.objects.create(booking=self.booking)
        Payment.objects.filter(pk=payment.pk).update(paid_at=timezone.now())
        with self.assertRaisesMessage(payments.FlowConflict, 'already been paid'):
            self.start()
        Payment.objects.filter(pk=payment.pk).update(refunded_at=timezone.now())
        with self.assertRaisesMessage(payments.FlowConflict, 'already been paid'):
            self.start()

    # ---- reuse / session rules
    def test_recent_session_is_reused(self):
        first = self.start()
        second = self.start()
        self.assertFalse(first.reused)
        self.assertTrue(second.reused)
        self.assertEqual(first.url, second.url)
        self.assertEqual(self.create.call_count, 1)
        self.assertEqual(PaymentAttempt.objects.count(), 1)

    def test_old_session_is_replaced_by_a_new_one(self):
        self.create.side_effect = [make_intention(5001, client_secret='cs_1'),
                                   make_intention(5002, client_secret='cs_2')]
        self.start()
        self.age_attempts(payments.ATTEMPT_REUSE_SECONDS + 5)
        second = self.start()
        self.assertFalse(second.reused)
        self.assertIn('clientSecret=cs_2', second.url)
        self.assertEqual(PaymentAttempt.objects.count(), 2)
        self.assertEqual(Payment.objects.count(), 1)

    def test_a_session_being_created_right_now_blocks_a_second_click(self):
        payment = Payment.objects.create(booking=self.booking)
        PaymentAttempt.objects.create(payment=payment, special_reference='fh-x-1')  # no secret yet
        with self.assertRaisesMessage(payments.FlowConflict, 'being created'):
            self.start()
        self.create.assert_not_called()

    def test_an_abandoned_half_created_session_does_not_block_forever(self):
        payment = Payment.objects.create(booking=self.booking)
        PaymentAttempt.objects.create(payment=payment, special_reference='fh-x-1')
        self.age_attempts(payments.CREATING_WINDOW_SECONDS + 5)
        checkout = self.start()
        self.assertFalse(checkout.reused)
        self.assertEqual(self.create.call_count, 1)

    def test_attempt_cap(self):
        payment = Payment.objects.create(booking=self.booking)
        for i in range(payments.MAX_ATTEMPTS_PER_PAYMENT):
            PaymentAttempt.objects.create(payment=payment, special_reference=f'old-{i}', client_secret='x')
        self.age_attempts(3600)
        with self.assertRaisesMessage(payments.FlowConflict, 'Too many'):
            self.start()
        self.create.assert_not_called()

    def test_one_attempt_below_the_cap_is_still_allowed(self):
        payment = Payment.objects.create(booking=self.booking)
        for i in range(payments.MAX_ATTEMPTS_PER_PAYMENT - 1):
            PaymentAttempt.objects.create(payment=payment, special_reference=f'old-{i}', client_secret='x')
        self.age_attempts(3600)
        self.assertFalse(self.start().reused)

    # ---- gateway failure
    def test_gateway_failure_leaves_no_half_created_attempt(self):
        self.create.side_effect = paymob.PaymobError('network error: Timeout')
        with self.assertRaises(payments.FlowGatewayDown):
            self.start()
        self.assertEqual(PaymentAttempt.objects.count(), 0)
        # and the customer can simply try again
        self.create.side_effect = None
        self.create.return_value = make_intention(5002, client_secret='cs_2')
        self.assertIn('clientSecret=cs_2', self.start().url)
