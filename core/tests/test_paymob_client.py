import json
from decimal import Decimal
from unittest import mock

import requests
from django.test import override_settings

from core import paymob

from .helpers import FarhetiTestCase, sign_obj, webhook_obj


def fake_response(status=200, data=None, text=None, bad_json=False):
    resp = mock.Mock()
    resp.status_code = status
    resp.text = text if text is not None else json.dumps(data)
    if bad_json:
        resp.json.side_effect = ValueError('not json')
    else:
        resp.json.return_value = data
    return resp


class VerifyHmacTests(FarhetiTestCase):
    def setUp(self):
        super().setUp()
        self.obj = webhook_obj()
        self.sig = sign_obj(self.obj)

    def test_valid_signature(self):
        self.assertTrue(paymob.verify_transaction_hmac(self.obj, self.sig))

    def test_case_and_padding_are_tolerated(self):
        self.assertTrue(paymob.verify_transaction_hmac(self.obj, self.sig.upper()))
        self.assertTrue(paymob.verify_transaction_hmac(self.obj, f'  {self.sig} '))

    def test_changing_any_signed_field_invalidates(self):
        for key, value in (('amount_cents', 1), ('success', False), ('id', 1002),
                           ('currency', 'USD'), ('pending', True), ('order', {'id': 1}),
                           ('source_data', {'pan': '0000', 'sub_type': 'x', 'type': 'card'})):
            with self.subTest(field=key):
                tampered = dict(self.obj)
                tampered[key] = value
                self.assertFalse(paymob.verify_transaction_hmac(tampered, self.sig))

    def test_missing_or_malformed_signature(self):
        for bad in (None, '', 123, b'abc', 'é' * 128, 'x' * 128):
            with self.subTest(sig=bad):
                self.assertFalse(paymob.verify_transaction_hmac(self.obj, bad))

    def test_signature_made_with_another_secret(self):
        self.assertFalse(paymob.verify_transaction_hmac(self.obj, sign_obj(self.obj, 'other')))

    @override_settings(PAYMOB_HMAC_SECRET='')
    def test_unconfigured_secret_rejects_everything(self):
        with self.assertLogs('farheti.payments', 'CRITICAL'):
            self.assertFalse(paymob.verify_transaction_hmac(self.obj, sign_obj(self.obj, '')))


class ToCentsTests(FarhetiTestCase):
    def test_conversion(self):
        for amount, cents in (('1000.00', 100000), ('10.05', 1005), ('0.01', 1), ('99999999.99', 9999999999)):
            with self.subTest(amount=amount):
                self.assertEqual(paymob.to_cents(Decimal(amount)), cents)

    def test_checkout_url_carries_both_keys(self):
        url = paymob.checkout_url('cs_1')
        self.assertTrue(url.startswith('https://checkout.test/?'))
        self.assertIn('publicKey=pk_test', url)
        self.assertIn('clientSecret=cs_1', url)


class RefundTransactionTests(FarhetiTestCase):
    def call(self, response=None, side_effect=None):
        with mock.patch('core.paymob.requests.post', return_value=response, side_effect=side_effect) as post:
            result = paymob.refund_transaction(transaction_id=1001, amount_cents=100000)
        return result, post

    def test_success(self):
        result, post = self.call(fake_response(200, {'success': True, 'id': 77}))
        self.assertTrue(result.ok)
        self.assertEqual(result.reference, '77')
        kwargs = post.call_args.kwargs
        self.assertEqual(kwargs['json'], {'transaction_id': 1001, 'amount_cents': 100000})
        self.assertEqual(kwargs['headers']['Authorization'], 'Token sk_test')
        self.assertTrue(post.call_args.args[0].endswith('/api/acceptance/void_refund/refund'))

    def test_string_success_flag(self):
        result, _ = self.call(fake_response(200, {'success': 'true', 'id': 5}))
        self.assertTrue(result.ok)

    def test_declined_is_definitive(self):
        result, _ = self.call(fake_response(200, {'success': False}))
        self.assertFalse(result.ok)
        self.assertTrue(result.definitive)

    def test_4xx_is_definitive(self):
        result, _ = self.call(fake_response(400, {'detail': 'bad'}))
        self.assertFalse(result.ok)
        self.assertTrue(result.definitive)

    def test_5xx_is_unknown(self):
        result, _ = self.call(fake_response(503, {}))
        self.assertFalse(result.ok)
        self.assertFalse(result.definitive)

    def test_network_error_is_unknown_and_never_raises(self):
        result, _ = self.call(side_effect=requests.ConnectionError('down'))
        self.assertFalse(result.ok)
        self.assertFalse(result.definitive)

    def test_unclear_200_responses_are_unknown(self):
        for resp in (fake_response(200, {'success': True}),            # no id
                     fake_response(200, {'something': 'else'}),
                     fake_response(200, None, text='<html>', bad_json=True)):
            with self.subTest(text=resp.text):
                result, _ = self.call(resp)
                self.assertFalse(result.ok)
                self.assertFalse(result.definitive)


class CreateIntentionTests(FarhetiTestCase):
    KWARGS = dict(
        amount_cents=100000, special_reference='fh-1-abc', booking_id=7,
        item_name='Farheti booking #7', billing={'first_name': 'A'}, expiration_seconds=3600,
    )
    GOOD = {'intention_order_id': 5001, 'id': 'int_9', 'client_secret': 'cs_9'}

    def test_success_and_request_body(self):
        with mock.patch('core.paymob.requests.post', return_value=fake_response(201, self.GOOD)) as post:
            intention = paymob.create_intention(**self.KWARGS)
        self.assertEqual((intention.order_id, intention.intention_id, intention.client_secret),
                         (5001, 'int_9', 'cs_9'))
        body = post.call_args.kwargs['json']
        self.assertEqual(body['amount'], 100000)
        self.assertEqual(body['currency'], 'EGP')
        self.assertEqual(body['payment_methods'], [123])
        self.assertEqual(body['special_reference'], 'fh-1-abc')
        self.assertEqual(body['expiration'], 3600)
        self.assertEqual(body['extras'], {'booking_id': 7})
        self.assertNotIn('notification_url', body)
        self.assertNotIn('redirection_url', body)
        self.assertEqual(post.call_args.kwargs['headers']['Authorization'], 'Token sk_test')

    @override_settings(PAYMOB_NOTIFICATION_URL='https://x.test/n', PAYMOB_REDIRECTION_URL='https://x.test/r')
    def test_optional_urls_are_sent_when_configured(self):
        with mock.patch('core.paymob.requests.post', return_value=fake_response(200, self.GOOD)) as post:
            paymob.create_intention(**self.KWARGS)
        body = post.call_args.kwargs['json']
        self.assertEqual(body['notification_url'], 'https://x.test/n')
        self.assertEqual(body['redirection_url'], 'https://x.test/r')

    def test_failures_raise_paymob_error(self):
        cases = {
            'http 400': dict(return_value=fake_response(400, {'detail': 'no'})),
            'network': dict(side_effect=requests.Timeout('slow')),
            'no secret': dict(return_value=fake_response(200, {'intention_order_id': 1})),
            'bad json': dict(return_value=fake_response(200, None, text='x', bad_json=True)),
        }
        for name, kwargs in cases.items():
            with self.subTest(case=name):
                with mock.patch('core.paymob.requests.post', **kwargs):
                    with self.assertRaises(paymob.PaymobError):
                        paymob.create_intention(**self.KWARGS)
