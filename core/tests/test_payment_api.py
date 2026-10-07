from datetime import timedelta
from unittest import mock

from django.urls import reverse
from django.utils import timezone

from core import paymob
from core.models import Booking, Payment, Provider, RefundAttempt

from .helpers import (
    B, S, FarhetiTestCase, api, make_booking, make_intention, make_paid_booking, make_user,
    refund_failed, refund_ok,
)


class PayEndpointTests(FarhetiTestCase):
    def setUp(self):
        super().setUp()
        self.booking = make_booking(status=B.ACCEPTED)
        self.customer = self.booking.customer
        self.url = reverse('booking-pay', args=[self.booking.pk])
        patcher = mock.patch('core.paymob.create_intention', return_value=make_intention())
        self.create = patcher.start()
        self.addCleanup(patcher.stop)

    def test_requires_authentication(self):
        self.assertEqual(api().post(self.url, {}, format='json').status_code, 401)

    def test_customer_gets_a_checkout_url(self):
        resp = api(self.customer).post(self.url, {}, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('clientSecret=cs_1', resp.json()['checkout_url'])

    def test_phone_number_is_validated_and_forwarded(self):
        client = api(self.customer)
        self.assertEqual(client.post(self.url, {'phone_number': 'abc'}, format='json').status_code, 400)
        resp = client.post(self.url, {'phone_number': '+201001234567'}, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.create.call_args.kwargs['billing']['phone_number'], '+201001234567')

    def test_other_users_get_404(self):
        for user in (make_user(), self.booking.provider_service.provider.user):
            self.assertEqual(api(user).post(self.url, {}, format='json').status_code, 404)

    def test_out_of_range_id_is_404(self):
        resp = api(self.customer).post('/bookings/99999999999/pay/', {}, format='json')
        self.assertEqual(resp.status_code, 404)

    def test_not_payable_is_409(self):
        Booking.objects.filter(pk=self.booking.pk).update(status=B.PENDING)
        self.assertEqual(api(self.customer).post(self.url, {}, format='json').status_code, 409)

    def test_gateway_down_is_503(self):
        self.create.side_effect = paymob.PaymobError('down')
        resp = api(self.customer).post(self.url, {}, format='json')
        self.assertEqual(resp.status_code, 503)

    def test_payment_init_is_throttled(self):
        client = api(self.customer)
        for _ in range(10):
            self.assertEqual(client.post(self.url, {}, format='json').status_code, 200)
        self.assertEqual(client.post(self.url, {}, format='json').status_code, 429)


class CancelAndRefundEndpointTests(FarhetiTestCase):
    def setUp(self):
        super().setUp()
        self.booking, self.payment = make_paid_booking(days_ahead=10)
        self.customer = self.booking.customer
        self.provider_user = self.booking.provider_service.provider.user
        self.url = reverse('booking-cancel-and-refund', args=[self.booking.pk])
        patcher = mock.patch('core.paymob.refund_transaction', return_value=refund_ok('9001'))
        self.gateway = patcher.start()
        self.addCleanup(patcher.stop)

    def test_requires_authentication(self):
        self.assertEqual(api().post(self.url, {}, format='json').status_code, 401)

    def test_customer_refund(self):
        resp = api(self.customer).post(self.url, {}, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {'status': 'cancelled', 'refund': 'completed'})
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, B.CANCELLED)

    def test_provider_refund_uses_the_provider_role(self):
        resp = api(self.provider_user).post(self.url, {}, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(RefundAttempt.objects.get().requested_by_id, self.provider_user.pk)

    def test_strangers_and_staff_who_are_not_parties_get_404(self):
        for user in (make_user(), make_user(is_staff=True)):
            self.assertEqual(api(user).post(self.url, {}, format='json').status_code, 404)
        self.gateway.assert_not_called()

    def test_customer_inside_the_seven_day_window_is_403(self):
        Booking.objects.filter(pk=self.booking.pk).update(event_date=timezone.localdate() + timedelta(days=3))
        self.assertEqual(api(self.customer).post(self.url, {}, format='json').status_code, 403)
        self.gateway.assert_not_called()

    def test_not_paid_is_409(self):
        Booking.objects.filter(pk=self.booking.pk).update(status=B.ACCEPTED)
        self.assertEqual(api(self.customer).post(self.url, {}, format='json').status_code, 409)

    def test_gateway_failure_is_502_and_booking_stays_paid(self):
        self.gateway.return_value = refund_failed(definitive=True)
        resp = api(self.customer).post(self.url, {}, format='json')
        self.assertEqual(resp.status_code, 502)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, B.PAID)
        self.assertEqual(RefundAttempt.objects.get().status, S.FAILED)

    def test_refund_endpoint_is_throttled(self):
        self.gateway.return_value = refund_failed(definitive=True)
        client = api(self.customer)
        codes = [client.post(self.url, {}, format='json').status_code for _ in range(11)]
        self.assertEqual(codes[-1], 429)


class PaymentReadTests(FarhetiTestCase):
    def setUp(self):
        super().setUp()
        self.booking, self.payment = make_paid_booking(days_ahead=10)
        self.customer = self.booking.customer
        self.provider = self.booking.provider_service.provider
        self.provider_user = self.provider.user

    def detail(self, user, booking=None):
        booking = booking or self.booking
        return api(user).get(reverse('payment-detail', args=[booking.pk]))

    def test_customer_view_hides_commission_and_payout_fields(self):
        data = self.detail(self.customer).json()
        self.assertEqual((data['amount'], data['currency'], data['payment_status']), ('1000.00', 'EGP', 'paid'))
        self.assertEqual(data['booking'], self.booking.pk)
        for key in ('commission_amount', 'payout_amount', 'payout_status',
                    'payout_completed_at', 'payout_reference'):
            self.assertNotIn(key, data)

    def test_provider_view_shows_the_money_split(self):
        data = self.detail(self.provider_user).json()
        self.assertEqual((data['commission_amount'], data['payout_amount']), ('100.00', '900.00'))
        self.assertEqual(data['payout_status'], 'awaiting_event')

    def test_staff_sees_any_payment_but_strangers_get_404(self):
        self.assertEqual(self.detail(make_user(is_staff=True)).status_code, 200)
        self.assertEqual(self.detail(make_user()).status_code, 404)

    def test_unauthenticated_is_401(self):
        self.assertEqual(api().get(reverse('payment-detail', args=[self.booking.pk])).status_code, 401)

    def test_out_of_range_id_is_404(self):
        resp = api(self.customer).get('/payments/9999999999/')
        self.assertEqual(resp.status_code, 404)

    def test_payout_status_values(self):
        def status():
            return self.detail(self.provider_user).json()['payout_status']

        self.assertEqual(status(), 'awaiting_event')
        Booking.objects.filter(pk=self.booking.pk).update(event_date=timezone.localdate() - timedelta(days=1))
        self.assertEqual(status(), 'pending')
        Provider.objects.filter(pk=self.provider.pk).update(is_suspended=True)
        self.assertEqual(status(), 'on_hold')
        Provider.objects.filter(pk=self.provider.pk).update(is_suspended=False)
        Payment.objects.filter(pk=self.payment.pk).update(payout_completed=True)
        self.assertEqual(status(), 'paid_out')
        Payment.objects.filter(pk=self.payment.pk).update(payout_completed=False, refunded_at=timezone.now())
        self.assertEqual(status(), 'refunded')
        self.assertEqual(self.detail(self.provider_user).json()['payment_status'], 'refunded')

    def test_unpaid_payment_status(self):
        booking = make_booking(status=B.ACCEPTED, listing=self.booking.provider_service)
        Payment.objects.create(booking=booking)
        data = self.detail(self.provider_user, booking).json()
        self.assertEqual((data['payment_status'], data['payout_status']), ('unpaid', 'not_paid'))

    def test_list_is_scoped_to_the_user_and_filterable(self):
        other_booking, _ = make_paid_booking(txn_id='1002')
        url = reverse('payment-list')
        mine = api(self.customer).get(url).json()['results']
        self.assertEqual([row['booking'] for row in mine], [self.booking.pk])
        self.assertEqual(api(make_user()).get(url).json()['results'], [])
        staff = api(make_user(is_staff=True))
        self.assertEqual(staff.get(url).json()['count'], 2)
        filtered = staff.get(url, {'booking': other_booking.pk}).json()['results']
        self.assertEqual([row['booking'] for row in filtered], [other_booking.pk])

    def test_list_filter_validation(self):
        client = api(self.customer)
        for bad in ('abc', '0', '2147483648'):
            with self.subTest(value=bad):
                self.assertEqual(client.get(reverse('payment-list'), {'booking': bad}).status_code, 400)
