from django.contrib.auth.models import User
from django.urls import reverse

from core.models import Message, Provider

from .helpers import B, FarhetiTestCase, api, make_booking, make_listing, make_user


class MessageApiBase(FarhetiTestCase):
    def setUp(self):
        super().setUp()
        self.listing = make_listing()
        self.provider = self.listing.provider
        self.provider_user = self.provider.user
        self.customer = make_user()
        self.stranger = make_user()
        self.staff = make_user(is_staff=True)
        self.booking = make_booking(listing=self.listing, customer=self.customer, status=B.ACCEPTED)
        self.url = reverse('message-list')
        self.mark_read_url = reverse('message-mark-read')

    def send(self, user, content='hello', booking=None, **extra):
        data = {'booking': (booking or self.booking).pk, 'content': content}
        data.update(extra)
        return api(user).post(self.url, data, format='json')

    def add(self, sender, content='hi', booking=None):
        return Message.objects.create(booking=booking or self.booking, sender=sender, content=content)

    def list(self, user, **params):
        params.setdefault('booking', self.booking.pk)
        return api(user).get(self.url, params)


class MessageSendTests(MessageApiBase):
    def test_customer_and_provider_can_send(self):
        for user, role in ((self.customer, 'customer'), (self.provider_user, 'provider')):
            resp = self.send(user, f'from {role}')
            self.assertEqual(resp.status_code, 201)
            data = resp.json()
            self.assertEqual((data['content'], data['sender_role'], data['is_mine'], data['is_read']),
                             (f'from {role}', role, True, False))
            self.assertNotIn('sender', data)

    def test_requires_authentication(self):
        self.assertEqual(api().post(self.url, {'booking': self.booking.pk, 'content': 'x'}, format='json').status_code, 401)
        self.assertEqual(api().get(self.url, {'booking': self.booking.pk}).status_code, 401)

    def test_strangers_get_404(self):
        self.assertEqual(self.send(self.stranger).status_code, 404)
        self.assertFalse(Message.objects.exists())

    def test_staff_cannot_send(self):
        self.assertEqual(self.send(self.staff).status_code, 403)

    def test_messaging_is_open_only_while_the_booking_is_live(self):
        for status in (B.PENDING, B.ACCEPTED, B.PAID):
            with self.subTest(status=status):
                booking = make_booking(listing=self.listing, customer=self.customer, status=status)
                self.assertEqual(self.send(self.customer, booking=booking).status_code, 201)
        for status in (B.DECLINED, B.EXPIRED, B.CANCELLED, B.COMPLETED):
            with self.subTest(status=status):
                booking = make_booking(listing=self.listing, customer=self.customer, status=status)
                self.assertEqual(self.send(self.customer, booking=booking).status_code, 409)
                self.assertEqual(self.list(self.customer, booking=booking.pk).status_code, 200)  # history stays readable

    def test_content_validation_and_trimming(self):
        self.assertEqual(self.send(self.customer, '   ').status_code, 400)
        self.assertEqual(self.send(self.customer, '').status_code, 400)
        self.assertEqual(self.send(self.customer, 'x' * 2001).status_code, 400)
        self.assertEqual(self.send(self.customer, 'x' * 2000).status_code, 201)
        resp = self.send(self.customer, '  padded  ')
        self.assertEqual(resp.json()['content'], 'padded')

    def test_booking_reference_validation(self):
        for bad in (0, -1, 'abc', 2147483648, None):
            with self.subTest(booking=bad):
                resp = api(self.customer).post(self.url, {'booking': bad, 'content': 'x'}, format='json')
                self.assertEqual(resp.status_code, 400)

    def test_client_cannot_set_read_state_or_booking_through_the_body(self):
        resp = self.send(self.customer, is_read=True, id=999999)
        self.assertEqual(resp.status_code, 201)
        self.assertFalse(resp.json()['is_read'])
        self.assertNotEqual(resp.json()['id'], 999999)

    def test_customer_cannot_message_an_unavailable_provider(self):
        User.objects.filter(pk=self.provider_user.pk).update(is_active=False)
        resp = self.send(self.customer)
        self.assertEqual(resp.status_code, 409)

    def test_suspended_provider_can_read_but_not_send(self):
        self.add(self.customer, 'are you there?')
        Provider.objects.filter(pk=self.provider.pk).update(is_suspended=True)
        self.assertEqual(self.send(self.provider_user).status_code, 403)
        self.assertEqual(self.list(self.provider_user).status_code, 200)
        self.assertEqual(self.send(self.customer).status_code, 201)

    def test_sending_is_throttled(self):
        client = api(self.customer)
        body = {'booking': self.booking.pk, 'content': 'spam'}
        for _ in range(20):
            self.assertEqual(client.post(self.url, body, format='json').status_code, 201)
        self.assertEqual(client.post(self.url, body, format='json').status_code, 429)

    def test_strict_parser_rejects_lone_surrogates(self):
        body = '{"booking": %d, "content": "\\ud800"}' % self.booking.pk
        resp = api(self.customer).post(self.url, data=body, content_type='application/json')
        self.assertEqual(resp.status_code, 400)

    def test_strict_parser_rejects_oversized_bodies(self):
        resp = api(self.customer).post(
            self.url, {'booking': self.booking.pk, 'content': 'hi', 'padding': 'x' * 70000}, format='json'
        )
        self.assertEqual(resp.status_code, 400)


class MessageListTests(MessageApiBase):
    def setUp(self):
        super().setUp()
        self.m1 = self.add(self.customer, 'one')
        self.m2 = self.add(self.provider_user, 'two')
        self.m3 = self.add(self.customer, 'three')

    def ids(self, user, **params):
        return [row['id'] for row in self.list(user, **params).json()['results']]

    def test_newest_first_by_default_and_oldest_first_on_request(self):
        self.assertEqual(self.ids(self.customer), [self.m3.pk, self.m2.pk, self.m1.pk])
        self.assertEqual(self.ids(self.customer, ordering='created_at'), [self.m1.pk, self.m2.pk, self.m3.pk])

    def test_after_id_returns_only_newer_messages_oldest_first(self):
        self.assertEqual(self.ids(self.customer, after_id=self.m1.pk), [self.m2.pk, self.m3.pk])
        self.assertEqual(self.ids(self.customer, after_id=self.m3.pk), [])

    def test_after_id_cannot_be_combined_with_descending_order(self):
        resp = self.list(self.customer, after_id=self.m1.pk, ordering='-created_at')
        self.assertEqual(resp.status_code, 400)

    def test_is_mine_and_sender_role_depend_on_the_viewer(self):
        rows = {row['id']: row for row in self.list(self.provider_user).json()['results']}
        self.assertFalse(rows[self.m1.pk]['is_mine'])
        self.assertTrue(rows[self.m2.pk]['is_mine'])
        self.assertEqual(rows[self.m1.pk]['sender_role'], 'customer')
        self.assertEqual(rows[self.m2.pk]['sender_role'], 'provider')

    def test_booking_param_is_required_and_validated(self):
        client = api(self.customer)
        self.assertEqual(client.get(self.url).status_code, 400)
        for bad in ('abc', '0', '2147483648'):
            with self.subTest(booking=bad):
                self.assertEqual(client.get(self.url, {'booking': bad}).status_code, 400)
        self.assertEqual(client.get(self.url, {'booking': self.booking.pk, 'ordering': 'id'}).status_code, 400)

    def test_visibility(self):
        self.assertEqual(self.list(self.stranger).status_code, 404)
        self.assertEqual(self.list(self.staff).status_code, 200)
        other = make_booking(listing=self.listing, customer=self.stranger, status=B.ACCEPTED)
        self.assertEqual(self.list(self.customer, booking=other.pk).status_code, 404)

    def test_messages_never_leak_between_bookings(self):
        other = make_booking(listing=self.listing, customer=self.stranger, status=B.ACCEPTED)
        stray = self.add(self.stranger, 'private', booking=other)
        self.assertNotIn(stray.pk, self.ids(self.customer))
        self.assertNotIn(stray.pk, self.ids(self.provider_user))


class MessageMarkReadTests(MessageApiBase):
    def test_marks_only_the_other_partys_unread_messages(self):
        theirs = self.add(self.provider_user, 'from provider')
        mine = self.add(self.customer, 'from me')
        resp = api(self.customer).post(self.mark_read_url, {'booking': self.booking.pk}, format='json')
        self.assertEqual((resp.status_code, resp.json()), (200, {'marked_read': 1}))
        theirs.refresh_from_db()
        mine.refresh_from_db()
        self.assertTrue(theirs.is_read)
        self.assertFalse(mine.is_read)
        again = api(self.customer).post(self.mark_read_url, {'booking': self.booking.pk}, format='json')
        self.assertEqual(again.json(), {'marked_read': 0})

    def test_access_rules(self):
        body = {'booking': self.booking.pk}
        self.assertEqual(api().post(self.mark_read_url, body, format='json').status_code, 401)
        self.assertEqual(api(self.stranger).post(self.mark_read_url, body, format='json').status_code, 404)
        self.assertEqual(api(self.staff).post(self.mark_read_url, body, format='json').status_code, 403)
        self.assertEqual(api(self.customer).post(self.mark_read_url, {'booking': 0}, format='json').status_code, 400)
