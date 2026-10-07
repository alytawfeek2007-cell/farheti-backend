"""Round 4: email verification, password reset, the verification gate, transactional mail, reminders."""
import re
import time
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs, urlparse

from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.core import mail
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from core import emails, jobs
from core.models import Booking, Category, EmailVerification, Payment, Service
from core.tests.helpers import (
    FarhetiTestCase, FarhetiTransactionTestCase, S, B, api, days_from_today,
    make_accepted_with_attempt, make_booking, make_listing, make_paid_booking, make_provider,
    make_refund_attempt, make_user, post_webhook, run_concurrently, values, webhook_obj,
)
from core.views import IsEmailVerified

NEW_PASSWORD = 'Zebra-Quilt-8842'
OLD_PASSWORD = 'Str0ng-Pass-123'


def link_params(message):
    url = re.search(r'https?://\S+', message.body).group(0)
    return {key: found[0] for key, found in parse_qs(urlparse(url).query).items()}


def unverified_user():
    user = make_user(verified=False)
    EmailVerification.objects.create(user=user)
    return user


def verify_now(user):
    EmailVerification.objects.update_or_create(user=user, defaults={'verified_at': timezone.now()})


def no_ip_throttle():
    return mock.patch('rest_framework.throttling.ScopedRateThrottle.allow_request', return_value=True)


# ---------------------------------------------------------------- verification
class VerifyEmailTests(FarhetiTestCase):
    def verify(self, token):
        return self.client.post(reverse('verify-email'), {'token': token}, format='json')

    def test_valid_token_verifies_the_user(self):
        user = unverified_user()
        response = self.verify(emails.make_verification_token(user))
        self.assertEqual(response.status_code, 200)
        user.email_verification.refresh_from_db()
        self.assertIsNotNone(user.email_verification.verified_at)

    def test_verifying_twice_is_harmless_and_keeps_the_first_timestamp(self):
        user = unverified_user()
        token = emails.make_verification_token(user)
        self.assertEqual(self.verify(token).status_code, 200)
        first = EmailVerification.objects.get(user=user).verified_at
        self.assertEqual(self.verify(token).status_code, 200)
        self.assertEqual(EmailVerification.objects.get(user=user).verified_at, first)

    def test_user_without_a_row_gets_one(self):
        user = make_user(verified=False)
        self.assertEqual(self.verify(emails.make_verification_token(user)).status_code, 200)
        self.assertIsNotNone(EmailVerification.objects.get(user=user).verified_at)

    def test_garbage_and_tampered_tokens_are_rejected(self):
        user = unverified_user()
        token = emails.make_verification_token(user)
        self.assertEqual(self.verify('garbage').status_code, 400)
        self.assertEqual(self.verify(token[:-2] + 'xx').status_code, 400)
        self.assertEqual(self.client.post(reverse('verify-email'), {}, format='json').status_code, 400)
        self.assertIsNone(EmailVerification.objects.get(user=user).verified_at)

    def test_token_is_valid_for_two_days_but_not_four(self):
        user = unverified_user()
        with mock.patch('django.core.signing.time.time', return_value=time.time() - 2 * 24 * 3600):
            fresh_enough = emails.make_verification_token(user)
        with mock.patch('django.core.signing.time.time', return_value=time.time() - 4 * 24 * 3600):
            too_old = emails.make_verification_token(user)
        self.assertEqual(self.verify(too_old).status_code, 400)
        self.assertEqual(self.verify(fresh_enough).status_code, 200)

    def test_token_dies_when_the_email_changes(self):
        user = unverified_user()
        token = emails.make_verification_token(user)
        User.objects.filter(pk=user.pk).update(email='changed@example.com')
        self.assertEqual(self.verify(token).status_code, 400)

    def test_token_for_a_deactivated_user_is_rejected(self):
        user = unverified_user()
        token = emails.make_verification_token(user)
        User.objects.filter(pk=user.pk).update(is_active=False)
        self.assertEqual(self.verify(token).status_code, 400)

    def test_a_bad_authorization_header_is_ignored(self):
        user = unverified_user()
        self.client.credentials(HTTP_AUTHORIZATION='Bearer garbage')
        self.assertEqual(self.verify(emails.make_verification_token(user)).status_code, 200)


class ResendVerificationTests(FarhetiTestCase):
    def test_requires_authentication(self):
        self.assertEqual(self.client.post(reverse('resend-verification'), {}, format='json').status_code, 401)

    def test_sends_one_mail_and_a_second_call_within_a_minute_sends_none(self):
        user = unverified_user()
        client = api(user)
        with self.captureOnCommitCallbacks(execute=True):
            first = client.post(reverse('resend-verification'), {}, format='json')
            second = client.post(reverse('resend-verification'), {}, format='json')
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.data, second.data)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [user.email])
        self.assertEqual(emails.read_verification_token(link_params(mail.outbox[0])['token']), user)

    def test_user_without_a_row_is_handled(self):
        user = make_user(verified=False)
        with self.captureOnCommitCallbacks(execute=True):
            response = api(user).post(reverse('resend-verification'), {}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        self.assertTrue(EmailVerification.objects.filter(user=user).exists())

    def test_already_verified_user_gets_no_mail(self):
        user = make_user()
        with self.captureOnCommitCallbacks(execute=True):
            response = api(user).post(reverse('resend-verification'), {}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 0)

    def test_sixth_request_in_an_hour_is_throttled(self):
        client = api(unverified_user())
        codes = [client.post(reverse('resend-verification'), {}, format='json').status_code for _ in range(6)]
        self.assertEqual(codes, [200] * 5 + [429])


# ---------------------------------------------------------------- the verification gate
class VerificationGateTests(FarhetiTestCase):
    def booking_payload(self, listing):
        return {'provider_service': listing.pk, 'event_date': days_from_today(10).isoformat()}

    def test_unverified_customer_cannot_create_a_booking(self):
        listing, customer = make_listing(), make_user(verified=False)
        response = api(customer).post(reverse('booking-list'), self.booking_payload(listing), format='json')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data['detail'].code, 'email_not_verified')
        self.assertFalse(Booking.objects.exists())

    def test_verified_customer_can_create_a_booking(self):
        listing, customer = make_listing(), make_user()
        response = api(customer).post(reverse('booking-list'), self.booking_payload(listing), format='json')
        self.assertEqual(response.status_code, 201)

    def test_verifying_unlocks_booking(self):
        listing, customer = make_listing(), unverified_user()
        client = api(customer)
        self.assertEqual(client.post(reverse('booking-list'), self.booking_payload(listing), format='json').status_code, 403)
        verify_now(customer)
        self.assertEqual(client.post(reverse('booking-list'), self.booking_payload(listing), format='json').status_code, 201)

    def test_unverified_user_can_still_read(self):
        customer = make_user(verified=False)
        self.assertEqual(api(customer).get(reverse('booking-list')).status_code, 200)

    def test_unverified_user_cannot_send_messages_but_a_verified_one_can(self):
        customer = make_user(verified=False)
        booking = make_booking(customer=customer)
        payload = {'booking': booking.pk, 'content': 'hello'}
        denied = api(customer).post(reverse('message-list'), payload, format='json')
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.data['detail'].code, 'email_not_verified')
        verify_now(customer)
        self.assertEqual(api(customer).post(reverse('message-list'), payload, format='json').status_code, 201)

    def test_unverified_provider_cannot_create_a_listing(self):
        category = Category.objects.create(name='Gate category')
        service = Service.objects.create(category=category, name='Gate service')
        user = make_user(verified=False)
        make_provider(user)
        payload = {'service': service.pk, 'price': '150.00'}
        self.assertEqual(api(user).post(reverse('provider-service-list'), payload, format='json').status_code, 403)
        verify_now(user)
        self.assertEqual(api(user).post(reverse('provider-service-list'), payload, format='json').status_code, 201)

    def test_staff_are_exempt_and_users_without_a_row_are_not(self):
        staff = make_user(is_staff=True, verified=False)
        plain = make_user(verified=False)
        self.assertTrue(IsEmailVerified().has_permission(SimpleNamespace(user=staff), None))
        self.assertFalse(IsEmailVerified().has_permission(SimpleNamespace(user=plain), None))


# ---------------------------------------------------------------- password reset
class PasswordResetRequestTests(FarhetiTestCase):
    def request_reset(self, email):
        return self.client.post(reverse('password-reset'), {'email': email}, format='json')

    def test_known_email_gets_a_link_and_unknown_gets_the_identical_answer(self):
        user = make_user()
        with self.captureOnCommitCallbacks(execute=True):
            known = self.request_reset(user.email)
            unknown = self.request_reset('nobody@example.com')
        self.assertEqual(known.status_code, 200)
        self.assertEqual(known.status_code, unknown.status_code)
        self.assertEqual(known.data, unknown.data)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [user.email])
        params = link_params(mail.outbox[0])
        self.assertIn('uid', params)
        self.assertIn('token', params)
        self.assertIn(f'{settings.FRONTEND_BASE_URL}/reset-password', mail.outbox[0].body)

    def test_inactive_account_gets_no_mail(self):
        user = make_user(is_active=False)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.request_reset(user.email).status_code, 200)
        self.assertEqual(len(mail.outbox), 0)

    def test_email_lookup_is_case_insensitive(self):
        user = make_user()
        with self.captureOnCommitCallbacks(execute=True):
            self.request_reset(user.email.upper())
        self.assertEqual(len(mail.outbox), 1)

    def test_second_request_within_a_minute_sends_no_second_mail(self):
        user = make_user()
        with self.captureOnCommitCallbacks(execute=True):
            self.request_reset(user.email)
            self.request_reset(user.email)
        self.assertEqual(len(mail.outbox), 1)

    def test_invalid_email_format_is_a_400(self):
        self.assertEqual(self.request_reset('not-an-email').status_code, 400)

    def test_eleventh_request_in_an_hour_is_throttled(self):
        codes = [self.request_reset(f'nobody{n}@example.com').status_code for n in range(11)]
        self.assertEqual(codes, [200] * 10 + [429])


class PasswordResetConfirmTests(FarhetiTestCase):
    def start(self, user=None):
        user = user or make_user()
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse('password-reset'), {'email': user.email}, format='json')
        return user, link_params(mail.outbox[-1])

    def confirm(self, params, password=NEW_PASSWORD, **overrides):
        data = {'uid': params['uid'], 'token': params['token'], 'new_password': password}
        data.update(overrides)
        return self.client.post(reverse('password-reset-confirm'), data, format='json')

    def login(self, email, password):
        return self.client.post(reverse('login'), {'email': email, 'password': password}, format='json')

    def test_timeout_setting_is_one_hour(self):
        self.assertEqual(settings.PASSWORD_RESET_TIMEOUT, 3600)

    def test_reset_changes_the_password(self):
        user, params = self.start()
        self.assertEqual(self.confirm(params).status_code, 200)
        self.assertEqual(self.login(user.email, NEW_PASSWORD).status_code, 200)
        self.assertEqual(self.login(user.email, OLD_PASSWORD).status_code, 401)

    def test_the_link_works_only_once(self):
        user, params = self.start()
        self.assertEqual(self.confirm(params).status_code, 200)
        self.assertEqual(self.confirm(params, 'Another-Pass-7731').status_code, 400)
        self.assertEqual(self.login(user.email, NEW_PASSWORD).status_code, 200)

    def test_weak_password_is_rejected_and_the_link_stays_usable(self):
        user, params = self.start()
        weak = self.confirm(params, '12345678')
        self.assertEqual(weak.status_code, 400)
        self.assertIn('new_password', weak.data)
        self.assertEqual(self.confirm(params).status_code, 200)

    def test_expired_link_is_rejected(self):
        user, params = self.start()
        with mock.patch.object(PasswordResetTokenGenerator, '_now', return_value=datetime.now() + timedelta(hours=2)):
            self.assertEqual(self.confirm(params).status_code, 400)
        self.assertEqual(self.login(user.email, OLD_PASSWORD).status_code, 200)

    def test_link_just_under_an_hour_old_still_works(self):
        user, params = self.start()
        with mock.patch.object(PasswordResetTokenGenerator, '_now', return_value=datetime.now() + timedelta(minutes=59)):
            self.assertEqual(self.confirm(params).status_code, 200)

    def test_reset_kills_every_existing_refresh_token(self):
        user, params = self.start()
        refresh = RefreshToken.for_user(user)
        self.assertEqual(self.confirm(params).status_code, 200)
        response = self.client.post(reverse('token-refresh'), {'refresh': str(refresh)}, format='json')
        self.assertEqual(response.status_code, 401)

    def test_reset_clears_the_login_lockout(self):
        user, params = self.start()
        with no_ip_throttle():
            for _ in range(5):
                self.login(user.email, 'wrong-password-1')
            self.assertEqual(self.login(user.email, OLD_PASSWORD).status_code, 429)
            self.assertEqual(self.confirm(params).status_code, 200)
            self.assertEqual(self.login(user.email, NEW_PASSWORD).status_code, 200)

    def test_bad_links_are_400_never_500(self):
        user, params = self.start()
        other = make_user()
        self.assertEqual(self.confirm(params, uid='!!!').status_code, 400)
        huge = emails.urlsafe_base64_encode(b'99999999999999999999')
        self.assertEqual(self.confirm(params, uid=huge).status_code, 400)
        missing = emails.urlsafe_base64_encode(b'2000000000')
        self.assertEqual(self.confirm(params, uid=missing).status_code, 400)
        self.assertEqual(self.confirm(params, token='not-a-token').status_code, 400)
        other_uid = emails.urlsafe_base64_encode(str(other.pk).encode())
        self.assertEqual(self.confirm(params, uid=other_uid).status_code, 400)
        self.assertEqual(self.client.post(reverse('password-reset-confirm'), {}, format='json').status_code, 400)
        self.assertEqual(self.login(user.email, OLD_PASSWORD).status_code, 200)

    def test_inactive_user_cannot_reset(self):
        user, params = self.start()
        User.objects.filter(pk=user.pk).update(is_active=False)
        self.assertEqual(self.confirm(params).status_code, 400)


# ---------------------------------------------------------------- booking mails
class BookingMailTests(FarhetiTestCase):
    def test_accepting_emails_the_customer(self):
        booking = make_booking(days_ahead=10)
        provider = booking.provider_service.provider
        with self.captureOnCommitCallbacks(execute=True):
            response = api(provider.user).post(reverse('booking-accept', args=[booking.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, [booking.customer.email])
        self.assertIn('accepted', message.subject.lower())
        self.assertIn(provider.business_name, message.subject)
        self.assertIn('24 hours', message.body)
        self.assertIn(f'/bookings/{booking.pk}', message.body)
        self.assertEqual(len(message.alternatives), 1)

    def test_a_failed_accept_sends_nothing(self):
        listing = make_listing()
        make_booking(listing=listing, status=B.ACCEPTED, days_ahead=10)
        pending = make_booking(listing=listing, days_ahead=10)
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            response = api(listing.provider.user).post(reverse('booking-accept', args=[pending.pk]))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(callbacks, [])
        self.assertEqual(len(mail.outbox), 0)

    def test_declining_emails_the_customer(self):
        booking = make_booking(days_ahead=10)
        with self.captureOnCommitCallbacks(execute=True):
            response = api(booking.provider_service.provider.user).post(reverse('booking-decline', args=[booking.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [booking.customer.email])
        self.assertIn('declined', mail.outbox[0].subject.lower())

    def test_payment_emails_customer_and_provider_and_hides_the_customers_address_from_the_provider(self):
        booking, payment, attempt = make_accepted_with_attempt(order_id=5001)
        provider_email = booking.provider_service.provider.user.email
        obj = webhook_obj(txn_id=1001, amount_cents=100000, order_id=5001)
        with self.captureOnCommitCallbacks(execute=True):
            response = post_webhook(self.client, obj)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Booking.objects.get(pk=booking.pk).status, B.PAID)
        self.assertEqual({m.to[0] for m in mail.outbox}, {booking.customer.email, provider_email})
        provider_mail = next(m for m in mail.outbox if m.to[0] == provider_email)
        self.assertNotIn(booking.customer.email, provider_mail.body)

    def test_a_redelivered_webhook_sends_no_second_mail(self):
        make_accepted_with_attempt(order_id=5001)
        obj = webhook_obj(txn_id=1001, amount_cents=100000, order_id=5001)
        with self.captureOnCommitCallbacks(execute=True):
            post_webhook(self.client, obj)
        sent = len(mail.outbox)
        self.assertEqual(sent, 2)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(post_webhook(self.client, obj).status_code, 200)
        self.assertEqual(len(mail.outbox), sent)

    def test_a_dead_mail_server_never_breaks_the_webhook(self):
        booking, payment, attempt = make_accepted_with_attempt(order_id=5001)
        obj = webhook_obj(txn_id=1001, amount_cents=100000, order_id=5001)
        with mock.patch('django.core.mail.EmailMultiAlternatives.send', side_effect=OSError('smtp down')):
            with self.captureOnCommitCallbacks(execute=True):
                response = post_webhook(self.client, obj)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Booking.objects.get(pk=booking.pk).status, B.PAID)
        self.assertEqual(len(mail.outbox), 0)

    def test_confirm_method_reports_whether_it_newly_confirmed(self):
        booking, payment, attempt = make_accepted_with_attempt()
        self.assertIs(payment.mark_paid_and_confirm_booking('777', Decimal('1000.00')), True)
        payment = Payment.objects.get(pk=payment.pk)
        self.assertIs(payment.mark_paid_and_confirm_booking('777', Decimal('1000.00')), False)


# ---------------------------------------------------------------- reminders
class ReminderTests(FarhetiTestCase):
    def run_job(self, **kwargs):
        with self.captureOnCommitCallbacks(execute=True):
            return jobs.send_payment_reminders(**kwargs)

    def test_reminder_goes_out_once(self):
        booking = make_booking(status=B.ACCEPTED, deadline_in=timedelta(hours=5))
        first = self.run_job()
        self.assertEqual((first['candidates'], first['done']), (1, 1))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [booking.customer.email])
        self.assertIn('reminder', mail.outbox[0].subject.lower())
        self.assertIsNotNone(Booking.objects.get(pk=booking.pk).deadline_reminder_sent_at)
        second = self.run_job()
        self.assertEqual(second['candidates'], 0)
        self.assertEqual(len(mail.outbox), 1)

    def test_only_bookings_inside_the_window_are_reminded(self):
        make_booking(status=B.ACCEPTED, deadline_in=timedelta(hours=10))   # too early
        make_booking(status=B.ACCEPTED, deadline_in=timedelta(hours=-1))   # already expired
        make_booking(status=B.ACCEPTED, deadline_in=None)                  # no deadline
        make_booking(status=B.PENDING)
        make_booking(status=B.ACCEPTED, deadline_in=timedelta(hours=5), days_ahead=0)  # event day: cannot be paid
        make_booking(status=B.ACCEPTED, deadline_in=timedelta(hours=5), suspended=True)
        result = self.run_job()
        self.assertEqual(result['candidates'], 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_dry_run_sends_and_changes_nothing(self):
        booking = make_booking(status=B.ACCEPTED, deadline_in=timedelta(hours=5))
        result = self.run_job(dry_run=True)
        self.assertEqual((result['candidates'], result['done']), (1, 0))
        self.assertEqual(result['ids'], [booking.pk])
        self.assertIsNone(Booking.objects.get(pk=booking.pk).deadline_reminder_sent_at)
        self.assertEqual(len(mail.outbox), 0)

    def test_a_booking_paid_after_the_claim_is_not_reminded(self):
        booking = make_booking(status=B.ACCEPTED, deadline_in=timedelta(hours=5))
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            jobs.send_payment_reminders()
        Booking.objects.filter(pk=booking.pk).update(status=B.PAID)
        for callback in callbacks:
            callback()
        self.assertEqual(len(mail.outbox), 0)


class ReminderConcurrencyTests(FarhetiTransactionTestCase):
    def test_two_parallel_runs_send_exactly_one_reminder(self):
        make_booking(status=B.ACCEPTED, deadline_in=timedelta(hours=5))
        results = values(run_concurrently([
            lambda: jobs.send_payment_reminders(),
            lambda: jobs.send_payment_reminders(),
        ]))
        self.assertEqual(sum(r['done'] for r in results), 1)
        self.assertEqual(len(mail.outbox), 1)


# ---------------------------------------------------------------- owner alert
@override_settings(OWNER_ALERT_EMAILS=['owner@example.com'])
class OwnerAlertTests(FarhetiTestCase):
    def stuck_refund(self):
        booking, payment = make_paid_booking()
        return make_refund_attempt(payment, S.UNKNOWN)

    def test_stuck_refund_emails_the_owner_once_per_alert_window(self):
        attempt = self.stuck_refund()
        first = jobs.process_refunds()
        self.assertEqual(first['alerted_total'], 1)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['owner@example.com'])
        self.assertIn(f'attempt #{attempt.pk}', mail.outbox[0].body)
        jobs.process_refunds()
        self.assertEqual(len(mail.outbox), 1)

    def test_dry_run_sends_no_alert(self):
        self.stuck_refund()
        jobs.process_refunds(dry_run=True)
        self.assertEqual(len(mail.outbox), 0)

    def test_nothing_stuck_means_no_mail(self):
        make_paid_booking()
        jobs.process_refunds()
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(OWNER_ALERT_EMAILS=[])
    def test_no_configured_owner_means_no_mail_and_no_crash(self):
        self.stuck_refund()
        result = jobs.process_refunds()
        self.assertEqual(result['alerted_total'], 1)
        self.assertEqual(len(mail.outbox), 0)

    def test_a_dead_mail_server_does_not_break_the_job(self):
        self.stuck_refund()
        with mock.patch('django.core.mail.EmailMultiAlternatives.send', side_effect=OSError('smtp down')):
            result = jobs.process_refunds()
        self.assertEqual(result['stuck_total'], 1)