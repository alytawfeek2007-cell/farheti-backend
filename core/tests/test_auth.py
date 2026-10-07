"""Registration, login + lockout, logout and refresh (the auth tests round 3 skipped)."""
from unittest import mock

from django.contrib.auth.models import User
from django.core import mail
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from core.models import Provider
from core.tests.helpers import FarhetiTestCase, make_user

GOOD_PASSWORD = 'Zebra-Quilt-8842'
HELPER_PASSWORD = 'Str0ng-Pass-123'  # the password make_user() gives every user


def no_ip_throttle():
    """Switches off the per-IP scoped throttle so the per-email lockout can be tested alone."""
    return mock.patch('rest_framework.throttling.ScopedRateThrottle.allow_request', return_value=True)


class RegistrationTests(FarhetiTestCase):
    def post_customer(self, **overrides):
        data = {'email': 'new.customer@example.com', 'password': GOOD_PASSWORD}
        data.update(overrides)
        return self.client.post(reverse('register-customer'), data, format='json')

    def post_provider(self, **overrides):
        data = {'email': 'new.provider@example.com', 'password': GOOD_PASSWORD, 'business_name': 'Nile Events'}
        data.update(overrides)
        return self.client.post(reverse('register-provider'), data, format='json')

    def test_customer_registration_returns_tokens_and_an_unverified_row(self):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.post_customer()
        self.assertEqual(response.status_code, 201)
        self.assertIn('access', response.data)
        self.assertIn('refresh', response.data)
        user = User.objects.get(email='new.customer@example.com')
        self.assertTrue(user.check_password(GOOD_PASSWORD))
        self.assertIsNone(user.email_verification.verified_at)
        self.assertFalse(Provider.objects.filter(user=user).exists())
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['new.customer@example.com'])

    def test_provider_registration_creates_an_unapproved_provider_and_sends_mail(self):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.post_provider()
        self.assertEqual(response.status_code, 201)
        user = User.objects.get(email='new.provider@example.com')
        provider = Provider.objects.get(user=user)
        self.assertEqual(provider.business_name, 'Nile Events')
        self.assertFalse(provider.is_verified)
        self.assertIsNone(user.email_verification.verified_at)
        self.assertEqual(len(mail.outbox), 1)

    def test_provider_registration_requires_a_business_name(self):
        response = self.post_provider(business_name='')
        self.assertEqual(response.status_code, 400)
        self.assertFalse(User.objects.filter(email='new.provider@example.com').exists())

    def test_duplicate_email_is_rejected_case_insensitively_without_saying_why(self):
        existing = make_user()
        response = self.post_customer(email=existing.email.upper())
        self.assertEqual(response.status_code, 400)
        self.assertIn('Unable to register', str(response.data))
        self.assertEqual(User.objects.filter(email__iexact=existing.email).count(), 1)

    def test_weak_password_is_rejected(self):
        response = self.post_customer(password='12345678')
        self.assertEqual(response.status_code, 400)
        self.assertIn('password', response.data)
        self.assertFalse(User.objects.filter(email='new.customer@example.com').exists())

    def test_invalid_email_is_rejected(self):
        self.assertEqual(self.post_customer(email='not-an-email').status_code, 400)

    def test_password_with_surrounding_spaces_can_log_in_with_the_same_input(self):
        spaced = f' {GOOD_PASSWORD} '
        self.assertEqual(self.post_customer(password=spaced).status_code, 201)
        response = self.client.post(
            reverse('login'), {'email': 'new.customer@example.com', 'password': spaced}, format='json'
        )
        self.assertEqual(response.status_code, 200)

    def test_registration_still_succeeds_when_the_mail_server_is_down(self):
        with mock.patch('django.core.mail.EmailMultiAlternatives.send', side_effect=OSError('smtp down')):
            with self.captureOnCommitCallbacks(execute=True):
                response = self.post_customer()
        self.assertEqual(response.status_code, 201)
        self.assertTrue(User.objects.filter(email='new.customer@example.com').exists())
        self.assertEqual(len(mail.outbox), 0)


class LoginTests(FarhetiTestCase):
    def login(self, email, password):
        return self.client.post(reverse('login'), {'email': email, 'password': password}, format='json')

    def test_login_returns_tokens(self):
        user = make_user()
        response = self.login(user.email, HELPER_PASSWORD)
        self.assertEqual(response.status_code, 200)
        self.assertIn('access', response.data)
        self.assertIn('refresh', response.data)

    def test_login_email_is_case_insensitive(self):
        user = make_user()
        self.assertEqual(self.login(user.email.upper(), HELPER_PASSWORD).status_code, 200)

    def test_unverified_user_can_still_log_in(self):
        user = make_user(verified=False)
        self.assertEqual(self.login(user.email, HELPER_PASSWORD).status_code, 200)

    def test_wrong_password_unknown_email_and_inactive_user_are_all_401(self):
        user = make_user()
        inactive = make_user(is_active=False)
        self.assertEqual(self.login(user.email, 'wrong-password-1').status_code, 401)
        self.assertEqual(self.login('nobody@example.com', HELPER_PASSWORD).status_code, 401)
        self.assertEqual(self.login(inactive.email, HELPER_PASSWORD).status_code, 401)

    def test_five_failures_lock_the_account_even_for_the_right_password(self):
        user = make_user()
        with no_ip_throttle():
            for _ in range(5):
                self.assertEqual(self.login(user.email, 'wrong-password-1').status_code, 401)
            locked = self.login(user.email, HELPER_PASSWORD)
        self.assertEqual(locked.status_code, 429)
        self.assertGreaterEqual(int(locked.headers['Retry-After']), 1)

    def test_unknown_emails_lock_too_so_accounts_cannot_be_enumerated(self):
        with no_ip_throttle():
            for _ in range(5):
                self.assertEqual(self.login('nobody@example.com', 'wrong-password-1').status_code, 401)
            self.assertEqual(self.login('nobody@example.com', 'wrong-password-1').status_code, 429)

    def test_the_lock_is_per_email(self):
        locked_user, other_user = make_user(), make_user()
        with no_ip_throttle():
            for _ in range(5):
                self.login(locked_user.email, 'wrong-password-1')
            self.assertEqual(self.login(locked_user.email, HELPER_PASSWORD).status_code, 429)
            self.assertEqual(self.login(other_user.email, HELPER_PASSWORD).status_code, 200)

    def test_a_successful_login_clears_the_failure_counter(self):
        user = make_user()
        with no_ip_throttle():
            for _ in range(4):
                self.login(user.email, 'wrong-password-1')
            self.assertEqual(self.login(user.email, HELPER_PASSWORD).status_code, 200)
            for _ in range(4):
                self.assertEqual(self.login(user.email, 'wrong-password-1').status_code, 401)
            self.assertEqual(self.login(user.email, HELPER_PASSWORD).status_code, 200)

    def test_per_ip_login_throttle_stops_the_sixth_request_in_a_minute(self):
        statuses = [self.login(f'nobody{n}@example.com', 'wrong-password-1').status_code for n in range(6)]
        self.assertEqual(statuses[:5], [401] * 5)
        self.assertEqual(statuses[5], 429)


class LogoutAndRefreshTests(FarhetiTestCase):
    def authed_client(self, refresh):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f'Bearer {refresh.access_token}')
        return client

    def test_logout_blacklists_the_refresh_token(self):
        refresh = RefreshToken.for_user(make_user())
        response = self.authed_client(refresh).post(reverse('logout'), {'refresh': str(refresh)}, format='json')
        self.assertEqual(response.status_code, 205)
        again = self.client.post(reverse('token-refresh'), {'refresh': str(refresh)}, format='json')
        self.assertEqual(again.status_code, 401)

    def test_logout_requires_authentication(self):
        refresh = RefreshToken.for_user(make_user())
        response = self.client.post(reverse('logout'), {'refresh': str(refresh)}, format='json')
        self.assertEqual(response.status_code, 401)

    def test_logout_rejects_missing_or_garbage_refresh_tokens(self):
        refresh = RefreshToken.for_user(make_user())
        client = self.authed_client(refresh)
        self.assertEqual(client.post(reverse('logout'), {}, format='json').status_code, 400)
        self.assertEqual(client.post(reverse('logout'), {'refresh': 'garbage'}, format='json').status_code, 400)

    def test_logout_cannot_blacklist_someone_elses_token(self):
        mine = RefreshToken.for_user(make_user())
        theirs = RefreshToken.for_user(make_user())
        response = self.authed_client(mine).post(reverse('logout'), {'refresh': str(theirs)}, format='json')
        self.assertEqual(response.status_code, 400)
        still_works = self.client.post(reverse('token-refresh'), {'refresh': str(theirs)}, format='json')
        self.assertEqual(still_works.status_code, 200)

    def test_refresh_rotates_and_the_old_refresh_token_dies(self):
        refresh = RefreshToken.for_user(make_user())
        first = self.client.post(reverse('token-refresh'), {'refresh': str(refresh)}, format='json')
        self.assertEqual(first.status_code, 200)
        self.assertIn('access', first.data)
        self.assertIn('refresh', first.data)
        replay = self.client.post(reverse('token-refresh'), {'refresh': str(refresh)}, format='json')
        self.assertEqual(replay.status_code, 401)

    def test_refresh_rejects_garbage(self):
        response = self.client.post(reverse('token-refresh'), {'refresh': 'garbage'}, format='json')
        self.assertEqual(response.status_code, 401)