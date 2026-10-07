"""Shared builders and helpers for the Farheti test-suite. Nothing in here is a test."""
import hashlib
import hmac
import itertools
import logging
import threading
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal
from unittest import mock
from urllib.parse import urlencode

from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import connections
from django.db.models.query import QuerySet
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from core import paymob
from core.models import (
    Booking, Category, Payment, PaymentAttempt, Provider, ProviderService, RefundAttempt, Service, EmailVerification,
)

# Expected money/refund log lines go to farheti.* loggers; keep the test output clean.
logging.getLogger('farheti').addHandler(logging.NullHandler())

B = Booking.Status
S = RefundAttempt.Status
K = RefundAttempt.Kind

HMAC_SECRET = 'hmac-test-secret'

TEST_SETTINGS = {
    'CACHES': {
        'default': {
            'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
            'LOCATION': 'farheti-tests',
        }
    },
    'PAYMOB_SECRET_KEY': 'sk_test',
    'PAYMOB_PUBLIC_KEY': 'pk_test',
    'PAYMOB_HMAC_SECRET': HMAC_SECRET,
    'PAYMOB_CARD_INTEGRATION_ID': '123',
    'PAYMOB_BASE_URL': 'https://paymob.test',
    'PAYMOB_CHECKOUT_URL': 'https://checkout.test',
    'PAYMOB_NOTIFICATION_URL': '',
    'PAYMOB_REDIRECTION_URL': '',
    'PASSWORD_HASHERS': ['django.contrib.auth.hashers.MD5PasswordHasher'],
}


@override_settings(**TEST_SETTINGS)
class FarhetiTestCase(TestCase):
    client_class = APIClient

    def setUp(self):
        super().setUp()
        cache.clear()


@override_settings(**TEST_SETTINGS)
class FarhetiTransactionTestCase(TransactionTestCase):
    client_class = APIClient

    def setUp(self):
        super().setUp()
        cache.clear()


_counter = itertools.count(1)


def api(user=None):
    client = APIClient()
    if user is not None:
        client.force_authenticate(user=user)
    return client


def days_from_today(n):
    return timezone.localdate() + timedelta(days=n)


# ---------------------------------------------------------------- data builders
def make_user(*, is_staff=False, is_superuser=False, is_active=True, verified=True):
    n = next(_counter)
    user = User.objects.create_user(
        username=f'user{n}', email=f'user{n}@example.com', password='Str0ng-Pass-123',
        is_staff=is_staff, is_superuser=is_superuser, is_active=is_active,
    )
    if verified:
        EmailVerification.objects.create(user=user, verified_at=timezone.now())
    return user


def make_provider(user=None, *, verified=True, active=True, suspended=False, commission='10.00'):
    n = next(_counter)
    return Provider.objects.create(
        user=user or make_user(), business_name=f'Business {n}', commission_rate=Decimal(commission),
        is_verified=verified, is_active=active, is_suspended=suspended,
    )


def make_listing(provider=None, *, price='1000.00', commission='10.00', **provider_kwargs):
    provider = provider or make_provider(commission=commission, **provider_kwargs)
    n = next(_counter)
    category = Category.objects.create(name=f'Category {n}')
    service = Service.objects.create(category=category, name=f'Service {n}')
    return ProviderService.objects.create(provider=provider, service=service, price=Decimal(price))


BREAK_MODES = (
    'unverified', 'suspended', 'paused', 'login_disabled',
    'service_inactive', 'category_inactive', 'listing_inactive',
)


def break_listing(listing, how):
    """Make a listing non-bookable in one specific way."""
    provider = listing.provider
    if how == 'unverified':
        Provider.objects.filter(pk=provider.pk).update(is_verified=False)
    elif how == 'suspended':
        Provider.objects.filter(pk=provider.pk).update(is_suspended=True)
    elif how == 'paused':
        Provider.objects.filter(pk=provider.pk).update(is_active=False)
    elif how == 'login_disabled':
        User.objects.filter(pk=provider.user_id).update(is_active=False)
    elif how == 'service_inactive':
        Service.objects.filter(pk=listing.service_id).update(is_active=False)
    elif how == 'category_inactive':
        Category.objects.filter(pk=listing.service.category_id).update(is_active=False)
    elif how == 'listing_inactive':
        ProviderService.objects.filter(pk=listing.pk).update(is_active=False)
    else:
        raise ValueError(how)


def make_booking(*, listing=None, customer=None, status=B.PENDING, days_ahead=10,
                 deadline_in=timedelta(hours=24), price='1000.00', commission='10.00',
                 **provider_kwargs):
    listing = listing or make_listing(price=price, commission=commission, **provider_kwargs)
    customer = customer or make_user()
    booking = Booking.objects.create(
        customer=customer, provider_service=listing, event_date=days_from_today(days_ahead),
    )
    if status != B.PENDING:
        fields = {'status': status}
        if status in (B.ACCEPTED, B.PAID, B.COMPLETED):
            now = timezone.now()
            fields['accepted_at'] = now
            fields['payment_deadline'] = None if deadline_in is None else now + deadline_in
        Booking.objects.filter(pk=booking.pk).update(**fields)
    return Booking.objects.get(pk=booking.pk)


def make_paid_booking(*, txn_id='1001', days_ahead=10, **kwargs):
    booking = make_booking(status=B.ACCEPTED, days_ahead=days_ahead, **kwargs)
    payment = Payment.objects.create(booking=booking)
    Payment.objects.filter(pk=payment.pk).update(paid_at=timezone.now(), paymob_transaction_id=txn_id)
    Booking.objects.filter(pk=booking.pk).update(status=B.PAID)
    return Booking.objects.get(pk=booking.pk), Payment.objects.get(pk=payment.pk)


def make_accepted_with_attempt(order_id=5001, **kwargs):
    booking = make_booking(status=B.ACCEPTED, **kwargs)
    payment = Payment.objects.create(booking=booking)
    attempt = PaymentAttempt.objects.create(
        payment=payment, special_reference=f'fh-{payment.pk}-{next(_counter)}',
        paymob_order_id=order_id, client_secret='cs_test',
    )
    return booking, payment, attempt


def make_refund_attempt(payment, status=S.UNKNOWN, *, txn=None, kind=K.CANCELLATION,
                        started_ago=timedelta(minutes=10), amount=None, **fields):
    txn = txn or payment.paymob_transaction_id or f'9{next(_counter)}'
    started_at = None if started_ago is None else timezone.now() - started_ago
    return RefundAttempt.objects.create(
        payment=payment, kind=kind, status=status, paymob_transaction_id=txn,
        amount=amount or payment.amount, started_at=started_at, **fields,
    )


# ---------------------------------------------------------------- Paymob boundary fakes
def refund_ok(reference='9001'):
    return paymob.RefundResult(True, reference=reference)


def refund_failed(definitive=True, error='declined'):
    return paymob.RefundResult(False, definitive, error=error)


def make_intention(order_id=5001, intention_id='int_1', client_secret='cs_1'):
    return paymob.Intention(order_id=order_id, intention_id=intention_id, client_secret=client_secret)


# ---------------------------------------------------------------- webhook payloads
# Deliberately a hard-coded copy of Paymob's documented field order, NOT imported from
# core.paymob, so that a change in the production list makes the tests fail.
_HMAC_ORDER = (
    'amount_cents', 'created_at', 'currency', 'error_occured', 'has_parent_transaction', 'id',
    'integration_id', 'is_3d_secure', 'is_auth', 'is_capture', 'is_refunded',
    'is_standalone_payment', 'is_voided', 'order.id', 'owner', 'pending',
    'source_data.pan', 'source_data.sub_type', 'source_data.type', 'success',
)


def _text(value):
    if isinstance(value, bool):
        return 'true' if value else 'false'
    return '' if value is None else str(value)


def _dig(data, path):
    for key in path.split('.'):
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def sign_obj(obj, secret=HMAC_SECRET):
    message = ''.join(_text(_dig(obj, path)) for path in _HMAC_ORDER)
    return hmac.new(secret.encode(), message.encode(), hashlib.sha512).hexdigest()


def webhook_obj(*, txn_id=1001, amount_cents=100000, order_id=5001, currency='EGP',
                success=True, pending=False, error_occured=False, **overrides):
    obj = {
        'id': txn_id, 'amount_cents': amount_cents, 'created_at': '2026-10-04T10:00:00.000000',
        'currency': currency, 'error_occured': error_occured, 'has_parent_transaction': False,
        'integration_id': 123, 'is_3d_secure': True, 'is_auth': False, 'is_capture': False,
        'is_refunded': False, 'is_standalone_payment': True, 'is_voided': False,
        'order': {'id': order_id}, 'owner': 77, 'pending': pending,
        'source_data': {'pan': '2346', 'sub_type': 'MasterCard', 'type': 'card'},
        'success': success,
    }
    obj.update(overrides)
    return obj


def post_webhook(client, obj, *, signature=None, payload_type='TRANSACTION', secret=HMAC_SECRET):
    if signature is None:
        signature = sign_obj(obj, secret)
    url = f"{reverse('paymob-webhook')}?{urlencode({'hmac': signature})}"
    return client.post(url, {'type': payload_type, 'obj': obj}, format='json')


# ---------------------------------------------------------------- lock-order spy
LOCK_RANK = {'provider': 0, 'providerservice': 1, 'booking': 2, 'payment': 3, 'refundattempt': 4}


@contextmanager
def record_locks():
    """Records the model of every select_for_update() call, in order."""
    order = []
    original = QuerySet.select_for_update

    def spy(self, *args, **kwargs):
        order.append(self.model._meta.model_name)
        return original(self, *args, **kwargs)

    with mock.patch.object(QuerySet, 'select_for_update', spy):
        yield order


# ---------------------------------------------------------------- real threads
def run_concurrently(funcs, timeout=60):
    """Run callables in real threads (own DB connection each), released together by a barrier."""
    barrier = threading.Barrier(len(funcs))
    results = [None] * len(funcs)

    def runner(index, func):
        try:
            barrier.wait(timeout=15)
            results[index] = ('ok', func())
        except BaseException as exc:  # noqa: BLE001 - reported to the test
            results[index] = ('err', exc)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=runner, args=(i, f)) for i, f in enumerate(funcs)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout)
    if any(thread.is_alive() for thread in threads):
        raise AssertionError('a worker thread is still running: probable deadlock')
    return results


def values(results):
    """Return the values; re-raise the first worker error."""
    out = []
    for kind, value in results:
        if kind == 'err':
            raise value
        out.append(value)
    return out
