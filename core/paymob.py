import hashlib
import hmac
import logging
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import urlencode

import requests
from django.conf import settings

logger = logging.getLogger('farheti.payments')


class PaymobError(Exception):
    """Intention creation failed. Nothing was charged."""


@dataclass(frozen=True)
class Intention:
    order_id: int
    intention_id: str
    client_secret: str


@dataclass(frozen=True)
class RefundResult:
    ok: bool
    definitive: bool = False  # only meaningful when ok is False: True = Paymob certainly did not refund
    reference: str = ''
    error: str = ''


def to_cents(amount: Decimal) -> int:
    return int((amount * 100).to_integral_value())


def _headers():
    return {'Authorization': f'Token {settings.PAYMOB_SECRET_KEY}', 'Content-Type': 'application/json'}


def _base():
    return settings.PAYMOB_BASE_URL.rstrip('/')


def checkout_url(client_secret: str) -> str:
    query = urlencode({'publicKey': settings.PAYMOB_PUBLIC_KEY, 'clientSecret': client_secret})
    return f"{settings.PAYMOB_CHECKOUT_URL.rstrip('/')}/?{query}"


def create_intention(*, amount_cents, special_reference, booking_id, item_name, billing, expiration_seconds):
    body = {
        'amount': amount_cents,
        'currency': 'EGP',
        'payment_methods': [int(settings.PAYMOB_CARD_INTEGRATION_ID)],
        'items': [{'name': item_name[:50], 'amount': amount_cents, 'description': item_name[:100], 'quantity': 1}],
        'billing_data': billing,
        'special_reference': special_reference,
        'extras': {'booking_id': booking_id},
        'expiration': expiration_seconds,
    }
    if settings.PAYMOB_NOTIFICATION_URL:
        body['notification_url'] = settings.PAYMOB_NOTIFICATION_URL
    if settings.PAYMOB_REDIRECTION_URL:
        body['redirection_url'] = settings.PAYMOB_REDIRECTION_URL
    try:
        resp = requests.post(f'{_base()}/v1/intention/', json=body, headers=_headers(), timeout=(5, 20))
    except requests.RequestException as exc:
        raise PaymobError(f'network error: {exc.__class__.__name__}') from exc
    if resp.status_code not in (200, 201):
        raise PaymobError(f'HTTP {resp.status_code}: {resp.text[:300]}')
    try:
        data = resp.json()
        return Intention(
            order_id=int(data['intention_order_id']),
            intention_id=str(data.get('id', ''))[:100],
            client_secret=str(data['client_secret'])[:255],
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise PaymobError('unexpected intention response') from exc


def refund_transaction(*, transaction_id: int, amount_cents: int) -> RefundResult:
    """Never raises. Distinguishes 'certainly not refunded' (definitive) from 'unknown'."""
    try:
        resp = requests.post(
            f'{_base()}/api/acceptance/void_refund/refund',
            json={'transaction_id': transaction_id, 'amount_cents': amount_cents},
            headers=_headers(),
            timeout=(5, 30),
        )
    except requests.RequestException as exc:
        return RefundResult(False, False, error=f'network error: {exc.__class__.__name__}')
    code = resp.status_code
    if code >= 500:
        return RefundResult(False, False, error=f'HTTP {code}')
    try:
        data = resp.json()
    except ValueError:
        data = None
    if code in (200, 201) and isinstance(data, dict):
        success = data.get('success')
        if success in (True, 'true', 'True') and data.get('id') is not None:
            return RefundResult(True, reference=str(data['id']))
        if success in (False, 'false', 'False'):
            return RefundResult(False, True, error=f'declined: {resp.text[:200]}')
        return RefundResult(False, False, error=f'unclear response: {resp.text[:200]}')
    if 400 <= code < 500:
        return RefundResult(False, True, error=f'HTTP {code}: {resp.text[:200]}')
    return RefundResult(False, False, error=f'unexpected HTTP {code}')


_HMAC_FIELDS = (
    'amount_cents', 'created_at', 'currency', 'error_occured', 'has_parent_transaction', 'id',
    'integration_id', 'is_3d_secure', 'is_auth', 'is_capture', 'is_refunded',
    'is_standalone_payment', 'is_voided', 'order.id', 'owner', 'pending',
    'source_data.pan', 'source_data.sub_type', 'source_data.type', 'success',
)


def _dig(data, path):
    for key in path.split('.'):
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def _as_text(value):
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if value is None:
        return ''
    return str(value)


def verify_transaction_hmac(obj, received) -> bool:
    secret = settings.PAYMOB_HMAC_SECRET
    if not secret:
        logger.critical('PAYMOB_HMAC_SECRET is not configured; rejecting every webhook')
        return False
    if not isinstance(received, str) or not received:
        return False
    try:
        message = ''.join(_as_text(_dig(obj, f)) for f in _HMAC_FIELDS)
        expected = hmac.new(secret.encode('utf-8'), message.encode('utf-8'), hashlib.sha512).hexdigest()
    except UnicodeError:
        return False
    return hmac.compare_digest(
        expected.encode('ascii'), received.strip().lower().encode('utf-8', 'replace')
    )