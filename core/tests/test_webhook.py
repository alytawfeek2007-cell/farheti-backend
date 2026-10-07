from datetime import timedelta
from unittest import mock

from django.urls import reverse
from django.utils import timezone

from core.models import Booking, Payment, RefundAttempt, RefundedCharge

from .helpers import (
    B, K, S, FarhetiTestCase, make_accepted_with_attempt, post_webhook, refund_failed,
    refund_ok, sign_obj, webhook_obj,
)


class WebhookTestBase(FarhetiTestCase):
    def setUp(self):
        super().setUp()
        self.booking, self.payment, self.attempt = make_accepted_with_attempt(order_id=5001)
        patcher = mock.patch('core.paymob.refund_transaction', return_value=refund_ok('9001'))
        self.refund = patcher.start()
        self.addCleanup(patcher.stop)

    def post(self, obj=None, **kwargs):
        return post_webhook(self.client, obj if obj is not None else webhook_obj(), **kwargs)

    def reload(self):
        self.booking.refresh_from_db()
        self.payment.refresh_from_db()


class WebhookSignatureTests(WebhookTestBase):
    def test_valid_signed_charge_confirms_the_booking(self):
        resp = self.post()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {'status': 'ok'})
        self.reload()
        self.assertEqual(self.booking.status, B.PAID)
        self.assertEqual(self.payment.paymob_transaction_id, '1001')
        self.refund.assert_not_called()

    def test_bad_signature_is_401_and_changes_nothing(self):
        resp = self.post(signature='0' * 128)
        self.assertEqual(resp.status_code, 401)
        self.reload()
        self.assertEqual(self.booking.status, B.ACCEPTED)
        self.assertIsNone(self.payment.paid_at)

    def test_missing_signature_is_401(self):
        resp = self.client.post(
            reverse('paymob-webhook'), {'type': 'TRANSACTION', 'obj': webhook_obj()}, format='json'
        )
        self.assertEqual(resp.status_code, 401)

    def test_signature_from_another_secret_is_401(self):
        self.assertEqual(self.post(secret='attacker-secret').status_code, 401)

    def test_payload_tampered_after_signing_is_401(self):
        obj = webhook_obj()
        signature = sign_obj(obj)
        obj['amount_cents'] = 1
        self.assertEqual(self.post(obj, signature=signature).status_code, 401)
        self.reload()
        self.assertEqual(self.booking.status, B.ACCEPTED)

    def test_unconfigured_secret_rejects_every_webhook(self):
        with self.settings(PAYMOB_HMAC_SECRET=''):
            with self.assertLogs('farheti.payments', 'CRITICAL'):
                resp = self.post(secret='')
        self.assertEqual(resp.status_code, 401)
        self.reload()
        self.assertEqual(self.booking.status, B.ACCEPTED)

    def test_garbage_authorization_header_is_ignored(self):
        self.client.credentials(HTTP_AUTHORIZATION='Bearer not-a-real-token')
        self.assertEqual(self.post().status_code, 200)


class WebhookIgnoredPayloadTests(WebhookTestBase):
    def assert_ignored(self, resp):
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {'status': 'ignored'})
        self.reload()
        self.assertEqual(self.booking.status, B.ACCEPTED)
        self.assertIsNone(self.payment.paid_at)
        self.refund.assert_not_called()

    def test_non_transaction_type_is_ignored_before_signature_check(self):
        self.assert_ignored(self.post(payload_type='TOKEN', signature='bad'))

    def test_obj_that_is_not_a_dict_is_ignored(self):
        resp = self.client.post(
            reverse('paymob-webhook'), {'type': 'TRANSACTION', 'obj': 'text'}, format='json'
        )
        self.assert_ignored(resp)

    def test_body_that_is_not_a_dict_is_400(self):
        resp = self.client.post(reverse('paymob-webhook'), [1, 2], format='json')
        self.assertEqual(resp.status_code, 400)

    def test_unsuccessful_transactions_are_ignored(self):
        for name, obj in (
            ('success false', webhook_obj(success=False)),
            ('pending', webhook_obj(pending=True)),
            ('error', webhook_obj(error_occured=True)),
        ):
            with self.subTest(case=name):
                self.assert_ignored(self.post(obj))

    def test_refund_void_and_auth_flags_are_ignored(self):
        for flag in ('has_parent_transaction', 'is_refunded', 'is_voided', 'is_auth', 'is_capture'):
            with self.subTest(flag=flag):
                self.assert_ignored(self.post(webhook_obj(**{flag: True})))

    def test_unusable_ids_or_amounts_are_ignored_even_when_signed(self):
        cases = {
            'string id': webhook_obj(txn_id='1001'),
            'bool id': webhook_obj(txn_id=True),
            'string amount': webhook_obj(amount_cents='100000'),
            'zero amount': webhook_obj(amount_cents=0),
            'negative amount': webhook_obj(amount_cents=-5),
            'huge id': webhook_obj(txn_id=10 ** 15),
            'string order id': webhook_obj(order_id='5001'),
        }
        for name, obj in cases.items():
            with self.subTest(case=name):
                self.assert_ignored(self.post(obj))

    def test_unknown_order_is_404_and_logged(self):
        with self.assertLogs('farheti.payments', 'CRITICAL') as logs:
            resp = self.post(webhook_obj(order_id=999999))
        self.assertEqual(resp.status_code, 404)
        self.assertIn('UNMATCHED PAYMOB CHARGE', logs.output[0])
        self.refund.assert_not_called()


class WebhookIdempotencyTests(WebhookTestBase):
    def test_redelivery_does_nothing_the_second_time(self):
        self.assertEqual(self.post().status_code, 200)
        self.reload()
        first_paid_at = self.payment.paid_at
        self.assertEqual(self.post().status_code, 200)
        self.reload()
        self.assertEqual(self.payment.paid_at, first_paid_at)
        self.assertEqual(self.booking.status, B.PAID)
        self.refund.assert_not_called()
        self.assertFalse(RefundAttempt.objects.exists())

    def test_redelivery_after_the_event_date_never_refunds_the_real_charge(self):
        self.post()
        Booking.objects.filter(pk=self.booking.pk).update(
            event_date=timezone.localdate() - timedelta(days=1)
        )
        self.assertEqual(self.post().status_code, 200)
        self.refund.assert_not_called()
        self.reload()
        self.assertEqual(self.booking.status, B.PAID)
        self.assertIsNone(self.payment.refunded_at)

    def test_redelivery_after_booking_completed_never_refunds(self):
        self.post()
        Booking.objects.filter(pk=self.booking.pk).update(status=B.COMPLETED)
        self.assertEqual(self.post().status_code, 200)
        self.refund.assert_not_called()


class WebhookAutoRefundTests(WebhookTestBase):
    def assert_refunded_not_confirmed(self, txn, cents=100000, booking_status=B.ACCEPTED):
        self.reload()
        self.assertEqual(self.booking.status, booking_status)
        self.assertIsNone(self.payment.paid_at)
        self.assertIsNone(self.payment.refunded_at)  # that field is only for a real, confirmed payment
        self.refund.assert_called_once_with(transaction_id=txn, amount_cents=cents)
        charge = RefundedCharge.objects.get(paymob_transaction_id=str(txn))
        self.assertEqual(charge.refund_reference, '9001')
        attempt = RefundAttempt.objects.get(paymob_transaction_id=str(txn))
        self.assertEqual((attempt.kind, attempt.status), (K.UNCONFIRMED, S.DONE))

    def test_amount_mismatch_is_refunded(self):
        resp = self.post(webhook_obj(amount_cents=50000))
        self.assertEqual(resp.status_code, 200)
        self.assert_refunded_not_confirmed(1001, cents=50000)
        self.assertEqual(RefundedCharge.objects.get().amount, 500)

    def test_late_payment_is_refunded(self):
        Booking.objects.filter(pk=self.booking.pk).update(
            payment_deadline=timezone.now() - timedelta(minutes=5)
        )
        self.assertEqual(self.post().status_code, 200)
        self.assert_refunded_not_confirmed(1001)

    def test_event_date_today_is_refunded(self):
        Booking.objects.filter(pk=self.booking.pk).update(event_date=timezone.localdate())
        self.assertEqual(self.post().status_code, 200)
        self.assert_refunded_not_confirmed(1001)

    def test_wrong_currency_is_refunded(self):
        self.assertEqual(self.post(webhook_obj(currency='USD')).status_code, 200)
        self.assert_refunded_not_confirmed(1001)

    def test_charge_on_a_closed_booking_is_refunded(self):
        self.reload()
        for i, status in enumerate((B.CANCELLED, B.DECLINED, B.EXPIRED), start=1):
            with self.subTest(status=status):
                booking, payment, _ = make_accepted_with_attempt(order_id=6000 + i)
                Booking.objects.filter(pk=booking.pk).update(status=status)
                txn = 7000 + i
                resp = self.post(webhook_obj(txn_id=txn, order_id=6000 + i))
                self.assertEqual(resp.status_code, 200)
                booking.refresh_from_db()
                payment.refresh_from_db()
                self.assertEqual(booking.status, status)
                self.assertIsNone(payment.paid_at)
                self.refund.assert_called_with(transaction_id=txn, amount_cents=100000)
                self.assertTrue(RefundedCharge.objects.filter(paymob_transaction_id=str(txn)).exists())

    def test_second_charge_on_an_already_paid_booking_is_refunded(self):
        self.post(webhook_obj(txn_id=1001))
        resp = self.post(webhook_obj(txn_id=1002))
        self.assertEqual(resp.status_code, 200)
        self.reload()
        self.assertEqual(self.booking.status, B.PAID)
        self.assertEqual(self.payment.paymob_transaction_id, '1001')
        self.assertIsNone(self.payment.refunded_at)
        self.refund.assert_called_once_with(transaction_id=1002, amount_cents=100000)
        self.assertTrue(RefundedCharge.objects.filter(paymob_transaction_id='1002').exists())

    def test_redelivered_unconfirmed_charge_is_not_refunded_twice(self):
        self.post(webhook_obj(amount_cents=50000))
        self.post(webhook_obj(amount_cents=50000))
        self.post(webhook_obj(amount_cents=50000))
        self.assertEqual(self.refund.call_count, 1)
        self.assertEqual(RefundedCharge.objects.count(), 1)


class WebhookRefundFailureTests(WebhookTestBase):
    def test_definitive_failure_is_kept_for_retry_and_webhook_still_answers_200(self):
        self.refund.return_value = refund_failed(definitive=True, error='declined')
        resp = self.post(webhook_obj(amount_cents=50000))
        self.assertEqual(resp.status_code, 200)
        attempt = RefundAttempt.objects.get(paymob_transaction_id='1001')
        self.assertEqual((attempt.kind, attempt.status), (K.UNCONFIRMED, S.FAILED))
        self.assertIn('declined', attempt.last_error)
        self.assertFalse(RefundedCharge.objects.exists())
        self.reload()
        self.assertEqual(self.booking.status, B.ACCEPTED)

    def test_failed_twice_becomes_unknown_then_stops_calling_the_gateway(self):
        self.refund.return_value = refund_failed(definitive=True)
        obj = webhook_obj(amount_cents=50000)
        self.post(obj)                      # first failure: definitive
        self.post(obj)                      # repeat failure: no longer trusted as definitive
        attempt = RefundAttempt.objects.get(paymob_transaction_id='1001')
        self.assertEqual(attempt.status, S.UNKNOWN)
        self.assertEqual(self.refund.call_count, 2)
        self.post(obj)                      # UNKNOWN is parked: never re-sent automatically
        self.assertEqual(self.refund.call_count, 2)

    def test_network_failure_is_unknown_straight_away(self):
        self.refund.return_value = refund_failed(definitive=False, error='network error: Timeout')
        resp = self.post(webhook_obj(amount_cents=50000))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(RefundAttempt.objects.get().status, S.UNKNOWN)
        self.post(webhook_obj(amount_cents=50000))
        self.assertEqual(self.refund.call_count, 1)
        self.assertEqual(Payment.objects.get(pk=self.payment.pk).paid_at, None)
