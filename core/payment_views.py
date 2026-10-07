import logging
from decimal import Decimal

from django.db.models import Q
from django.utils import timezone
from rest_framework import serializers, viewsets
from rest_framework.exceptions import APIException, NotFound, PermissionDenied
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle, UserRateThrottle
from rest_framework.views import APIView

from . import payments
from . import paymob
from .models import Booking, Payment, PaymentAttempt
from .serializers import Conflict, MAX_DB_INT

logger = logging.getLogger('farheti.payments')


class GatewayUnavailable(APIException):
    status_code = 503
    default_detail = 'The payment provider is unavailable. Please try again shortly.'
    default_code = 'gateway_unavailable'


class RefundIncomplete(APIException):
    status_code = 502
    default_detail = 'The refund could not be completed right now.'
    default_code = 'refund_incomplete'


def _api_error(exc):
    if isinstance(exc, payments.FlowNotFound):
        return NotFound()
    if isinstance(exc, payments.FlowForbidden):
        return PermissionDenied(str(exc))
    if isinstance(exc, payments.FlowGatewayDown):
        return GatewayUnavailable()
    if isinstance(exc, payments.FlowRefundIncomplete):
        return RefundIncomplete(str(exc))
    return Conflict(str(exc))


def _check_id(pk):
    if pk < 1 or pk > MAX_DB_INT:
        raise NotFound()


class PayRequestSerializer(serializers.Serializer):
    phone_number = serializers.RegexField(r'^\+?[0-9]{8,15}$', required=False, max_length=16)


class BookingPayView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [UserRateThrottle, ScopedRateThrottle]
    throttle_scope = 'payment_init'

    def post(self, request, pk):
        _check_id(pk)
        serializer = PayRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            checkout = payments.start_checkout(
                booking_id=pk, user=request.user, phone=serializer.validated_data.get('phone_number')
            )
        except payments.PaymentFlowError as exc:
            raise _api_error(exc) from exc
        return Response({'checkout_url': checkout.url})


class BookingCancelAndRefundView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [UserRateThrottle, ScopedRateThrottle]
    throttle_scope = 'payment_refund'

    def post(self, request, pk):
        _check_id(pk)
        customer_id = (
            Booking.objects.filter(pk=pk)
            .filter(Q(customer=request.user) | Q(provider_service__provider__user=request.user))
            .values_list('customer_id', flat=True)
            .first()
        )
        if customer_id is None:
            raise NotFound()
        role = 'customer' if customer_id == request.user.pk else 'provider'
        try:
            payments.cancel_and_refund(booking_id=pk, actor=request.user, role=role)
        except payments.PaymentFlowError as exc:
            raise _api_error(exc) from exc
        return Response({'status': 'cancelled', 'refund': 'completed'})


_IGNORED_FLAGS = ('has_parent_transaction', 'is_refunded', 'is_voided', 'is_auth', 'is_capture')


class PaymobWebhookView(APIView):
    """Paymob 'transaction processed' callback. Only HMAC-signed fields are trusted."""
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = []

    def post(self, request):
        data = request.data
        if not isinstance(data, dict):
            return Response({'detail': 'invalid payload'}, status=400)
        obj = data.get('obj')
        if data.get('type') != 'TRANSACTION' or not isinstance(obj, dict):
            return Response({'status': 'ignored'})
        if not paymob.verify_transaction_hmac(obj, request.query_params.get('hmac')):
            logger.warning('Paymob webhook rejected: bad HMAC')
            return Response({'detail': 'invalid signature'}, status=401)

        # Signed fields only from here on.
        if obj.get('success') is not True or obj.get('pending') is not False or obj.get('error_occured') is True:
            return Response({'status': 'ignored'})
        if any(obj.get(flag) is True for flag in _IGNORED_FLAGS):
            return Response({'status': 'ignored'})

        txn_id = obj.get('id')
        amount_cents = obj.get('amount_cents')
        order = obj.get('order')
        order_id = order.get('id') if isinstance(order, dict) else None
        valid = all(
            isinstance(v, int) and not isinstance(v, bool)
            for v in (txn_id, amount_cents, order_id)
        )
        if not valid or not (0 < txn_id < 10**15) or not (0 < amount_cents < 10**10) or not (0 < order_id < 10**15):
            logger.error('Paymob webhook with unusable ids/amount (signature was valid)')
            return Response({'status': 'ignored'})

        attempt = PaymentAttempt.objects.select_related('payment').filter(paymob_order_id=order_id).first()
        if attempt is None:
            logger.critical(
                'UNMATCHED PAYMOB CHARGE txn=%s order=%s amount_cents=%s: investigate in Paymob dashboard',
                txn_id, order_id, amount_cents,
            )
            return Response({'detail': 'unknown order'}, status=404)

        outcome = payments.handle_successful_charge(
            payment=attempt.payment, txn_id=txn_id, amount_cents=amount_cents, currency=obj.get('currency'),
        )
        logger.info('Paymob txn %s for payment %s -> %s', txn_id, attempt.payment_id, outcome)
        return Response({'status': 'ok'})


# ---------------------------------------------------------------- read endpoints
PROVIDER_ONLY_FIELDS = ('commission_amount', 'payout_amount', 'payout_status', 'payout_completed_at', 'payout_reference')


class PaymentSerializer(serializers.ModelSerializer):
    booking = serializers.IntegerField(source='booking_id', read_only=True)
    currency = serializers.SerializerMethodField()
    payment_status = serializers.SerializerMethodField()
    commission_amount = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    payout_amount = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    payout_status = serializers.SerializerMethodField()

    class Meta:
        model = Payment
        fields = [
            'booking', 'amount', 'currency', 'payment_status', 'paid_at', 'refunded_at',
            'commission_amount', 'payout_amount', 'payout_status',
            'payout_completed_at', 'payout_reference', 'created_at',
        ]
        read_only_fields = fields

    def get_currency(self, obj):
        return 'EGP'

    def get_payment_status(self, obj):
        if obj.refunded_at is not None:
            return 'refunded'
        return 'paid' if obj.paid_at is not None else 'unpaid'

    def get_payout_status(self, obj):
        if obj.paid_at is None:
            return 'not_paid'
        if obj.refunded_at is not None:
            return 'refunded'
        if obj.payout_completed:
            return 'paid_out'
        booking = obj.booking
        provider = booking.provider_service.provider
        if provider.is_suspended or not provider.user.is_active:
            return 'on_hold'
        if booking.status == Booking.Status.PAID and booking.event_date >= timezone.localdate():
            return 'awaiting_event'
        if booking.status in (Booking.Status.PAID, Booking.Status.COMPLETED):
            return 'pending'
        return 'not_due'

    def to_representation(self, instance):
        data = super().to_representation(instance)
        request = self.context.get('request')
        if request is not None and instance.booking.customer_id == request.user.pk:
            for key in PROVIDER_ONLY_FIELDS:
                data.pop(key, None)
        return data


class _ListQuery(serializers.Serializer):
    booking = serializers.IntegerField(min_value=1, max_value=MAX_DB_INT, required=False)


class PaymentViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = PaymentSerializer
    lookup_field = 'booking_id'
    lookup_value_regex = '[0-9]{1,10}'

    def get_queryset(self):
        user = self.request.user
        qs = Payment.objects.select_related('booking__provider_service__provider__user')
        if not user.is_staff:
            qs = qs.filter(Q(booking__customer=user) | Q(booking__provider_service__provider__user=user))
        if self.action == 'list':
            query = _ListQuery(data=self.request.query_params)
            query.is_valid(raise_exception=True)
            booking = query.validated_data.get('booking')
            if booking is not None:
                qs = qs.filter(booking_id=booking)
        return qs.order_by('-created_at', '-id')

    def get_object(self):
        raw = self.kwargs[self.lookup_url_kwarg or self.lookup_field]
        if int(raw) > MAX_DB_INT:
            raise NotFound()
        return super().get_object()