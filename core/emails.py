"""
All outgoing mail goes through this module.

Rules:
- Mail is scheduled with transaction.on_commit, so a change that rolls back never emails.
- A mail failure is logged and swallowed. It must never break or roll back a money path,
  a booking action or the Paymob webhook.
- Booking mails re-read the booking when they are sent, so they describe the committed state.
- Plain text plus a simple HTML alternative. English only for now.
"""
import logging
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.auth.tokens import default_token_generator
from django.core import signing
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.utils import timezone
from django.utils.encoding import force_bytes, force_str
from django.utils.html import escape
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode

from .models import Booking, EmailVerification

logger = logging.getLogger('farheti.emails')

VERIFY_SALT = 'farheti.email-verification'
VERIFY_MAX_AGE_SECONDS = 3 * 24 * 3600
MAX_DB_INT = 2147483647


# ---------------------------------------------------------------- low level
def _link(path, **params):
    url = f'{settings.FRONTEND_BASE_URL}{path}'
    return f'{url}?{urlencode(params)}' if params else url


def _local(moment):
    if moment is None:
        return 'the deadline'
    return timezone.localtime(moment).strftime('%Y-%m-%d %H:%M %Z')


def _html(heading, paragraphs, button):
    parts = [f'<h2>{escape(heading)}</h2>']
    parts += [f'<p>{escape(paragraph)}</p>' for paragraph in paragraphs]
    if button:
        label, url = button
        parts.append(f'<p><a href="{escape(url)}">{escape(label)}</a></p>')
    return (
        '<div style="font-family:Arial,sans-serif;font-size:15px;line-height:1.5">'
        + ''.join(parts)
        + '</div>'
    )


def _send(to, subject, text, html=None):
    """Never raises. Returns True if the mailer accepted the message."""
    to = [address for address in to if address]
    if not to:
        return False
    try:
        message = EmailMultiAlternatives(subject=subject, body=text, to=to)
        if html:
            message.attach_alternative(html, 'text/html')
        message.send()
        return True
    except Exception:
        logger.exception('Email %r to %s could not be sent', subject, to)
        return False


def _compose(to, subject, heading, paragraphs, button=None):
    text = '\n\n'.join(paragraphs)
    if button:
        text += f'\n\n{button[0]}: {button[1]}'
    return _send(to, subject, text, _html(heading, paragraphs, button))


def _after_commit(func, *args):
    def run():
        try:
            func(*args)
        except Exception:
            logger.exception('Email task %s failed', getattr(func, '__name__', '?'))

    try:
        transaction.on_commit(run, robust=True)
    except Exception:
        logger.exception('Could not schedule email task %s', getattr(func, '__name__', '?'))


# ---------------------------------------------------------------- email verification
def make_verification_token(user):
    return signing.dumps({'uid': user.pk, 'email': user.email.lower()}, salt=VERIFY_SALT)


def read_verification_token(token):
    """Returns the active user the token was issued for, or None (bad, expired, email changed)."""
    try:
        data = signing.loads(token, salt=VERIFY_SALT, max_age=VERIFY_MAX_AGE_SECONDS)
        uid = data['uid']
        email = str(data['email'])
    except (signing.BadSignature, KeyError, TypeError):
        return None
    if not isinstance(uid, int) or isinstance(uid, bool) or not 1 <= uid <= MAX_DB_INT:
        return None
    user = User.objects.filter(pk=uid, is_active=True).first()
    if user is None or user.email.lower() != email:
        return None
    return user


def start_verification(user):
    _after_commit(_deliver_verification, user.pk)


def _deliver_verification(user_id):
    user = User.objects.filter(pk=user_id, is_active=True).first()
    if user is None or not user.email:
        return
    if EmailVerification.objects.filter(user=user, verified_at__isnull=False).exists():
        return
    link = _link('/verify-email', token=make_verification_token(user))
    days = VERIFY_MAX_AGE_SECONDS // 86400
    _compose(
        [user.email],
        'Verify your Farheti email address',
        'Verify your email address',
        [
            'Welcome to Farheti. Please confirm your email address so you can book services and send messages.',
            f'This link works for {days} days.',
        ],
        ('Verify email', link),
    )


# ---------------------------------------------------------------- password reset
def user_from_reset_uid(uidb64):
    try:
        pk = int(force_str(urlsafe_base64_decode(uidb64)))
    except (TypeError, ValueError, OverflowError, UnicodeDecodeError):
        return None
    if not 1 <= pk <= MAX_DB_INT:
        return None
    return User.objects.filter(pk=pk, is_active=True).first()


def start_password_reset(user):
    _after_commit(_deliver_password_reset, user.pk)


def _deliver_password_reset(user_id):
    user = User.objects.filter(pk=user_id, is_active=True).first()
    if user is None or not user.email:
        return
    link = _link(
        '/reset-password',
        uid=urlsafe_base64_encode(force_bytes(user.pk)),
        token=default_token_generator.make_token(user),
    )
    minutes = settings.PASSWORD_RESET_TIMEOUT // 60
    _compose(
        [user.email],
        'Reset your Farheti password',
        'Reset your password',
        [
            'We received a request to reset the password of your Farheti account.',
            f'This link works for {minutes} minutes and can be used once.',
            'If you did not ask for this, ignore this email. Your password will not change.',
        ],
        ('Choose a new password', link),
    )


# ---------------------------------------------------------------- booking mails
def notify_booking(booking_id, kind):
    """kind: 'accepted' | 'declined' | 'paid' | 'reminder'. Sent after the surrounding transaction commits."""
    _after_commit(_deliver_booking, booking_id, kind)


def _deliver_booking(booking_id, kind):
    booking = (
        Booking.objects.select_related(
            'customer', 'provider_service__service', 'provider_service__provider__user'
        )
        .filter(pk=booking_id)
        .first()
    )
    if booking is None:
        return
    provider = booking.provider_service.provider
    service = booking.provider_service.service.name
    event_date = booking.event_date.isoformat()
    ref = f'Booking #{booking.pk}'
    button = ('View booking', _link(f'/bookings/{booking.pk}'))
    customer_to = [booking.customer.email]
    provider_to = [provider.user.email]

    if kind == 'accepted':
        _compose(
            customer_to,
            f'{provider.business_name} accepted your booking request',
            'Your booking request was accepted',
            [
                f'{provider.business_name} accepted your request for {service} on {event_date} ({ref}).',
                f'Please pay {booking.price_snapshot} EGP within 24 hours (by {_local(booking.payment_deadline)}) '
                'to confirm the booking. If the payment is not completed in time, the request expires.',
            ],
            button,
        )
    elif kind == 'declined':
        _compose(
            customer_to,
            'Your booking request was declined',
            'Your booking request was declined',
            [f'{provider.business_name} could not take your request for {service} on {event_date} ({ref}).'],
            button,
        )
    elif kind == 'paid':
        _compose(
            customer_to,
            'Payment received for your booking',
            'Payment received',
            [f'We received your payment of {booking.price_snapshot} EGP for {service} with '
             f'{provider.business_name} on {event_date} ({ref}). Your booking is confirmed.'],
            button,
        )
        _compose(
            provider_to,
            'A booking was paid and is confirmed',
            'Booking confirmed',
            [f'The booking for {service} on {event_date} ({ref}) has been paid and is now confirmed.'],
            button,
        )
    elif kind == 'reminder':
        blocked = provider.is_suspended or not provider.user.is_active or not booking.customer.is_active
        if (
            booking.status != Booking.Status.ACCEPTED
            or booking.is_payment_expired
            or booking.event_date <= timezone.localdate()
            or blocked
        ):
            return
        _compose(
            customer_to,
            'Reminder: complete your payment',
            'Your booking is waiting for payment',
            [
                f'Your booking for {service} with {provider.business_name} on {event_date} ({ref}) '
                'is still waiting for payment.',
                f'The payment window closes at {_local(booking.payment_deadline)}. '
                'After that the request expires.',
            ],
            button,
        )
    else:
        logger.error('Unknown booking mail kind %r', kind)


# ---------------------------------------------------------------- owner alert
def send_owner_alert(lines, total):
    """One digest mail for refunds that need the owner. Called directly by the cron job."""
    recipients = list(getattr(settings, 'OWNER_ALERT_EMAILS', []) or [])
    if not recipients:
        logger.warning('OWNER_ALERT_EMAILS is empty: the stuck-refund alert was not emailed')
        return False
    paragraphs = [
        'These refunds are stuck and need you. Check the Paymob dashboard, then use the refund actions in admin.',
        *lines,
    ]
    if total > len(lines):
        paragraphs.append(f'{total - len(lines)} more are not listed here. See the server log.')
    return _compose(
        recipients,
        f'[Farheti] {total} refund(s) need your attention',
        'Refunds need your attention',
        paragraphs,
    )