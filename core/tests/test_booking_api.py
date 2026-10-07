from datetime import timedelta
from decimal import Decimal

from django.urls import reverse
from django.utils import timezone

from core.models import MIN_DAYS_AHEAD_TO_ACCEPT, Booking

from .helpers import (
    BREAK_MODES, B, FarhetiTestCase, api, break_listing, days_from_today, make_booking,
    make_listing, make_user,
)


class BookingApiBase(FarhetiTestCase):
    def setUp(self):
        super().setUp()
        self.listing = make_listing()
        self.provider = self.listing.provider
        self.provider_user = self.provider.user
        self.customer = make_user()
        self.other_customer = make_user()
        self.stranger = make_user()
        self.staff = make_user(is_staff=True)
        self.list_url = reverse('booking-list')

    def booking(self, **kwargs):
        kwargs.setdefault('listing', self.listing)
        kwargs.setdefault('customer', self.customer)
        return make_booking(**kwargs)

    def detail(self, booking):
        return reverse('booking-detail', args=[booking.pk])

    def action(self, name, booking):
        return reverse(f'booking-{name}', args=[booking.pk])

    def payload(self, listing=None, days=10, **extra):
        data = {'provider_service': (listing or self.listing).pk,
                'event_date': days_from_today(days).isoformat()}
        data.update(extra)
        return data

    def post_create(self, user, **kwargs):
        return api(user).post(self.list_url, self.payload(**kwargs), format='json')


class BookingCreateTests(BookingApiBase):
    def test_requires_authentication(self):
        self.assertEqual(api().post(self.list_url, self.payload(), format='json').status_code, 401)

    def test_customer_creates_a_pending_booking_with_snapshots(self):
        resp = self.post_create(self.customer, notes='Wedding hall')
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertEqual((data['status'], data['customer']), ('pending', self.customer.pk))
        self.assertEqual(data['price_snapshot'], '1000.00')
        self.assertNotIn('commission_rate_snapshot', data)  # customers never see the commission
        booking = Booking.objects.get(pk=data['id'])
        self.assertEqual(booking.commission_rate_snapshot, Decimal('10.00'))
        self.assertEqual(booking.notes, 'Wedding hall')

    def test_event_date_must_be_far_enough_ahead_to_be_accepted_and_paid(self):
        for days, expected in ((-1, 400), (0, 400), (1, 400),
                               (MIN_DAYS_AHEAD_TO_ACCEPT, 201), (MIN_DAYS_AHEAD_TO_ACCEPT + 1, 201)):
            with self.subTest(days_ahead=days):
                resp = self.post_create(make_user(), days=days)
                self.assertEqual(resp.status_code, expected)
                if expected == 400:
                    self.assertIn('event_date', resp.json())

    def test_invalid_or_missing_event_date(self):
        for bad in ('not-a-date', '', None):
            with self.subTest(value=bad):
                data = {'provider_service': self.listing.pk, 'event_date': bad}
                self.assertEqual(api(self.customer).post(self.list_url, data, format='json').status_code, 400)
        self.assertEqual(
            api(self.customer).post(self.list_url, {'provider_service': self.listing.pk}, format='json').status_code,
            400,
        )

    def test_client_cannot_set_server_controlled_fields(self):
        resp = api(self.customer).post(
            self.list_url,
            self.payload(status='paid', price_snapshot='1.00', customer=self.other_customer.pk,
                         commission_rate_snapshot='0.00', payment_deadline='2030-01-01T00:00:00Z'),
            format='json',
        )
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertEqual((data['status'], data['price_snapshot'], data['customer']),
                         ('pending', '1000.00', self.customer.pk))
        self.assertIsNone(data['payment_deadline'])

    def test_notes_length_limit(self):
        self.assertEqual(self.post_create(self.customer, notes='x' * 2001).status_code, 400)
        self.assertEqual(self.post_create(self.customer, notes='x' * 2000).status_code, 201)

    def test_unknown_listing_is_400(self):
        resp = api(self.customer).post(
            self.list_url, {'provider_service': 99999999, 'event_date': days_from_today(10).isoformat()},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn('provider_service', resp.json())

    def test_non_bookable_listings_are_rejected(self):
        for how in BREAK_MODES:
            with self.subTest(how=how):
                listing = make_listing()
                break_listing(listing, how)
                resp = self.post_create(self.customer, listing=listing)
                self.assertEqual(resp.status_code, 400)
                self.assertIn('provider_service', resp.json())

    def test_provider_cannot_book_their_own_service(self):
        resp = self.post_create(self.provider_user)
        self.assertEqual(resp.status_code, 400)
        self.assertIn('own service', str(resp.json()))

    def test_same_customer_cannot_double_book_a_provider_on_a_date(self):
        self.assertEqual(self.post_create(self.customer).status_code, 201)
        resp = self.post_create(self.customer)
        self.assertEqual(resp.status_code, 400)
        self.assertIn('event_date', resp.json())
        # a cancelled one no longer counts
        Booking.objects.filter(customer=self.customer).update(status=B.CANCELLED)
        self.assertEqual(self.post_create(self.customer).status_code, 201)

    def test_pending_requests_from_other_customers_do_not_block_a_date(self):
        self.assertEqual(self.post_create(self.customer).status_code, 201)
        self.assertEqual(self.post_create(self.other_customer).status_code, 201)

    def test_confirmed_booking_on_the_date_blocks_new_requests_with_409(self):
        for status in (B.ACCEPTED, B.PAID):
            with self.subTest(status=status):
                booked = make_listing(provider=self.provider)
                make_booking(listing=booked, customer=self.other_customer, status=status, days_ahead=20)
                resp = self.post_create(make_user(), days=20)
                self.assertEqual(resp.status_code, 409)

    def test_form_encoded_bodies_are_not_accepted(self):
        resp = api(self.customer).post(self.list_url, self.payload(), format='multipart')
        self.assertEqual(resp.status_code, 415)


class BookingReadTests(BookingApiBase):
    def test_visibility(self):
        mine = self.booking()
        other = self.booking(customer=self.other_customer)
        ids = lambda user: {row['id'] for row in api(user).get(self.list_url).json()['results']}  # noqa: E731
        self.assertEqual(ids(self.customer), {mine.pk})
        self.assertEqual(ids(self.provider_user), {mine.pk, other.pk})
        self.assertEqual(ids(self.stranger), set())
        self.assertEqual(ids(self.staff), {mine.pk, other.pk})
        self.assertEqual(api(self.stranger).get(self.detail(mine)).status_code, 404)
        self.assertEqual(api(self.customer).get(self.detail(other)).status_code, 404)
        self.assertEqual(api().get(self.list_url).status_code, 401)

    def test_commission_snapshot_is_visible_to_provider_and_staff_only(self):
        booking = self.booking()
        self.assertNotIn('commission_rate_snapshot', api(self.customer).get(self.detail(booking)).json())
        for user in (self.provider_user, self.staff):
            self.assertEqual(
                api(user).get(self.detail(booking)).json()['commission_rate_snapshot'], '10.00'
            )

    def test_filters_and_ordering(self):
        # provider_user is also a customer of someone else's listing
        elsewhere = make_listing()
        as_customer = make_booking(listing=elsewhere, customer=self.provider_user)
        incoming = self.booking(days_ahead=30)
        accepted = self.booking(customer=self.other_customer, status=B.ACCEPTED, days_ahead=20)
        client = api(self.provider_user)

        def ids(**params):
            return [row['id'] for row in client.get(self.list_url, params).json()['results']]

        self.assertEqual(set(ids(role='customer')), {as_customer.pk})
        self.assertEqual(set(ids(role='provider')), {incoming.pk, accepted.pk})
        self.assertEqual(ids(status='accepted'), [accepted.pk])
        self.assertEqual(ids(role='provider', ordering='event_date'), [accepted.pk, incoming.pk])
        self.assertEqual(ids(role='provider', ordering='-event_date'), [incoming.pk, accepted.pk])

    def test_invalid_filters_are_400(self):
        client = api(self.customer)
        for params in ({'role': 'admin'}, {'status': 'nope'}, {'ordering': 'price'}):
            with self.subTest(params=params):
                self.assertEqual(client.get(self.list_url, params).status_code, 400)

    def test_bookings_cannot_be_edited_or_deleted_directly(self):
        booking = self.booking()
        for method in ('put', 'patch', 'delete'):
            with self.subTest(method=method):
                resp = getattr(api(self.customer), method)(self.detail(booking), {}, format='json')
                self.assertEqual(resp.status_code, 405)


class BookingActionTests(BookingApiBase):
    def post(self, user, name, booking):
        return api(user).post(self.action(name, booking), {}, format='json')

    # ---- accept
    def test_provider_accepts_and_a_payment_window_opens(self):
        booking = self.booking()
        resp = self.post(self.provider_user, 'accept', booking)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data['status'], 'accepted')
        self.assertIsNotNone(data['payment_deadline'])
        self.assertFalse(data['is_payment_expired'])

    def test_only_the_provider_can_accept_decline(self):
        booking = self.booking()
        for name in ('accept', 'decline'):
            self.assertEqual(self.post(self.customer, name, booking).status_code, 403)
            self.assertEqual(self.post(self.staff, name, booking).status_code, 403)
            self.assertEqual(self.post(self.stranger, name, booking).status_code, 404)
        self.assertEqual(api().post(self.action('accept', booking), {}, format='json').status_code, 401)
        booking.refresh_from_db()
        self.assertEqual(booking.status, B.PENDING)

    def test_accept_twice_or_from_the_wrong_status_is_409(self):
        booking = self.booking()
        self.assertEqual(self.post(self.provider_user, 'accept', booking).status_code, 200)
        self.assertEqual(self.post(self.provider_user, 'accept', booking).status_code, 409)
        declined = self.booking(status=B.DECLINED)
        self.assertEqual(self.post(self.provider_user, 'accept', declined).status_code, 409)

    def test_accept_is_refused_when_the_event_is_too_close(self):
        booking = self.booking()
        Booking.objects.filter(pk=booking.pk).update(event_date=timezone.localdate() + timedelta(days=1))
        resp = self.post(self.provider_user, 'accept', booking)
        self.assertEqual(resp.status_code, 409)
        booking.refresh_from_db()
        self.assertEqual(booking.status, B.PENDING)

    def test_accept_is_refused_when_the_provider_is_no_longer_bookable(self):
        for how in ('unverified', 'suspended', 'login_disabled'):
            with self.subTest(how=how):
                listing = make_listing()
                booking = make_booking(listing=listing, customer=self.customer)
                break_listing(listing, how)
                resp = api(listing.provider.user).post(self.action('accept', booking), {}, format='json')
                self.assertEqual(resp.status_code, 409)

    def test_accept_is_refused_and_rolled_back_when_the_date_is_already_taken(self):
        booking = self.booking(days_ahead=15)
        other_listing = make_listing(provider=self.provider)
        make_booking(listing=other_listing, customer=self.other_customer, status=B.ACCEPTED, days_ahead=15)
        resp = self.post(self.provider_user, 'accept', booking)
        self.assertEqual(resp.status_code, 409)
        self.assertIn('another confirmed booking', str(resp.json()))
        booking.refresh_from_db()
        self.assertEqual(booking.status, B.PENDING)
        self.assertIsNone(booking.payment_deadline)

    # ---- decline
    def test_provider_declines_pending_only(self):
        booking = self.booking()
        self.assertEqual(self.post(self.provider_user, 'decline', booking).json()['status'], 'declined')
        accepted = self.booking(status=B.ACCEPTED)
        self.assertEqual(self.post(self.provider_user, 'decline', accepted).status_code, 409)

    # ---- cancel
    def test_customer_can_cancel_pending_and_accepted(self):
        for status in (B.PENDING, B.ACCEPTED):
            with self.subTest(status=status):
                booking = self.booking(status=status)
                resp = self.post(self.customer, 'cancel', booking)
                self.assertEqual((resp.status_code, resp.json()['status']), (200, 'cancelled'))

    def test_provider_can_cancel_accepted_but_must_decline_pending(self):
        accepted = self.booking(status=B.ACCEPTED)
        self.assertEqual(self.post(self.provider_user, 'cancel', accepted).status_code, 200)
        pending = self.booking()
        resp = self.post(self.provider_user, 'cancel', pending)
        self.assertEqual(resp.status_code, 409)
        self.assertIn('decline', str(resp.json()))

    def test_paid_bookings_cannot_be_cancelled_here(self):
        booking = self.booking(status=B.PAID)
        resp = self.post(self.customer, 'cancel', booking)
        self.assertEqual(resp.status_code, 409)
        self.assertIn('contact support', str(resp.json()))

    def test_other_statuses_cannot_be_cancelled_and_outsiders_cannot_cancel(self):
        for status in (B.DECLINED, B.EXPIRED, B.CANCELLED, B.COMPLETED):
            with self.subTest(status=status):
                booking = self.booking(status=status)
                self.assertEqual(self.post(self.customer, 'cancel', booking).status_code, 409)
        booking = self.booking()
        self.assertEqual(self.post(self.stranger, 'cancel', booking).status_code, 404)
        self.assertEqual(self.post(self.staff, 'cancel', booking).status_code, 403)

    # ---- complete
    def test_complete_after_the_event_by_either_party(self):
        for user in (self.customer, self.provider_user):
            with self.subTest(user=user.pk):
                booking = self.booking(status=B.PAID, days_ahead=-1)
                resp = self.post(user, 'complete', booking)
                self.assertEqual((resp.status_code, resp.json()['status']), (200, 'completed'))

    def test_complete_rules(self):
        future = self.booking(status=B.PAID, days_ahead=5)
        self.assertEqual(self.post(self.customer, 'complete', future).status_code, 409)
        not_paid = self.booking(status=B.ACCEPTED, days_ahead=-1)
        self.assertEqual(self.post(self.customer, 'complete', not_paid).status_code, 409)
        done = self.booking(status=B.PAID, days_ahead=-1)
        self.assertEqual(self.post(self.stranger, 'complete', done).status_code, 404)
        self.assertEqual(self.post(self.staff, 'complete', done).status_code, 403)
