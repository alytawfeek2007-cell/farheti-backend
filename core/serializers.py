import hashlib
import logging
import math
import time

from datetime import timedelta

from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import exceptions, serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from .models import (
    MIN_DAYS_AHEAD_TO_ACCEPT, Booking, Category, Provider, ProviderService, Service, Message, EmailVerification,
)

logger = logging.getLogger(__name__)

USERNAME_CONSTRAINT = 'auth_user_username_key'
EMAIL_CONSTRAINT = 'auth_user_email_lower_unique'

LOGIN_MAX_FAILURES = 5
LOGIN_FAILURE_WINDOW_SECONDS = 15 * 60
LOGIN_LOCK_SECONDS = 15 * 60
NO_SUCH_USERNAME = '@@no-such-user@@'

# Mirrors Provider.is_bookable + ProviderService.is_bookable in queryset form.
PUBLIC_PROVIDER_SERVICE_Q = Q(
    is_active=True,
    service__is_active=True,
    service__category__is_active=True,
    provider__is_active=True,
    provider__is_verified=True,
    provider__is_suspended=False,
    provider__user__is_active=True,
)

ACTIVE_BOOKING_STATUSES = (
    Booking.Status.PENDING,
    Booking.Status.ACCEPTED,
    Booking.Status.PAID,
)
# Only these statuses occupy a provider's date.
DATE_BLOCKING_STATUSES = (
    Booking.Status.ACCEPTED,
    Booking.Status.PAID,
)


class Conflict(exceptions.APIException):
    status_code = 409
    default_detail = 'Conflict.'
    default_code = 'conflict'


class CategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = ['id', 'name', 'slug', 'description', 'icon', 'is_active', 'created_at']
        read_only_fields = ['slug', 'created_at']

    def validate_name(self, value):
        qs = Category.objects.filter(name__iexact=value)
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("A category with this name already exists.")
        return value

    def update(self, instance, validated_data):
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save(update_fields=list(validated_data.keys()))
        return instance


class ServiceSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(source='category.name', read_only=True)

    class Meta:
        model = Service
        fields = ['id', 'category', 'category_name', 'name', 'description', 'is_active', 'created_at']
        read_only_fields = ['created_at']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # A new/changed category assignment must point at an active category.
        # But if we're editing an EXISTING service, its own current category
        # must remain a valid choice even if deactivated since, otherwise a
        # PUT with category left unchanged fails with "invalid pk".
        if isinstance(self.instance, Service):
            self.fields['category'].queryset = Category.objects.filter(
                Q(is_active=True) | Q(pk=self.instance.category_id)
            )
        else:
            self.fields['category'].queryset = Category.objects.filter(is_active=True)

    def validate(self, attrs):
        name = attrs.get('name') or getattr(self.instance, 'name', '')
        category = attrs.get('category') or getattr(self.instance, 'category', None)
        if name and category:
            qs = Service.objects.filter(name__iexact=name, category=category)
            if self.instance is not None:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise serializers.ValidationError(
                    {"name": "A service with this name already exists in this category."}
                )
        return attrs

    def update(self, instance, validated_data):
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save(update_fields=list(validated_data.keys()))
        return instance


class ProviderSerializer(serializers.ModelSerializer):
    """Full detail — used for the owning provider and for staff."""
    class Meta:
        model = Provider
        fields = [
            'id', 'user', 'business_name', 'bio', 'commission_rate',
            'is_verified', 'is_suspended', 'is_active', 'created_at',
        ]
        read_only_fields = ['user', 'commission_rate', 'is_verified', 'created_at']

    def get_fields(self):
        fields = super().get_fields()
        request = self.context.get('request')
        if not (request and request.user.is_authenticated and request.user.is_staff):
            fields['is_suspended'].read_only = True
        return fields

    def update(self, instance, validated_data):
        """
        Only writes the fields that actually changed, instead of a full
        instance.save() with every column — a full save would silently
        overwrite a concurrent change to a field NOT in this request (e.g.
        staff flipping is_suspended mid-flight during an owner's bio edit).
        """
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save(update_fields=list(validated_data.keys()))
        return instance


class PublicProviderSerializer(serializers.ModelSerializer):
    """
    Used for anyone browsing the marketplace who is neither the provider
    themself nor staff. Excludes commission_rate, user, and is_suspended.
    """
    class Meta:
        model = Provider
        fields = ['id', 'business_name', 'bio', 'is_active', 'created_at']
        read_only_fields = fields


class ProviderServiceSerializer(serializers.ModelSerializer):
    service = serializers.PrimaryKeyRelatedField(
        queryset=Service.objects.filter(is_active=True, category__is_active=True),
        error_messages={
            'does_not_exist': 'This service does not exist or is not available.',
        },
    )
    service_name = serializers.CharField(source='service.name', read_only=True)
    category = serializers.IntegerField(source='service.category_id', read_only=True)
    category_name = serializers.CharField(source='service.category.name', read_only=True)
    provider_business_name = serializers.CharField(source='provider.business_name', read_only=True)
    is_bookable = serializers.BooleanField(read_only=True)

    class Meta:
        model = ProviderService
        fields = [
            'id', 'provider', 'provider_business_name',
            'service', 'service_name', 'category', 'category_name',
            'price', 'is_active', 'is_bookable', 'created_at',
        ]
        read_only_fields = ['id', 'provider', 'created_at']
        validators = []

    def validate(self, attrs):
        if self.instance is None:
            request = self.context.get('request')
            provider = getattr(request.user, 'provider_profile', None) if request else None
            if provider is not None and ProviderService.objects.filter(
                provider=provider, service=attrs['service']
            ).exists():
                raise serializers.ValidationError({'service': 'You already offer this service.'})
        return attrs

    def update(self, instance, validated_data):
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        if validated_data:
            instance.save(update_fields=list(validated_data))
        return instance


class ProviderServiceUpdateSerializer(ProviderServiceSerializer):
    service = serializers.PrimaryKeyRelatedField(read_only=True)


class BookingSerializer(serializers.ModelSerializer):
    provider_service = serializers.PrimaryKeyRelatedField(
        queryset=ProviderService.objects.filter(PUBLIC_PROVIDER_SERVICE_Q),
        error_messages={
            'does_not_exist': 'This service is not available for booking.',
        },
    )
    provider = serializers.IntegerField(source='provider_service.provider_id', read_only=True)
    provider_business_name = serializers.CharField(
        source='provider_service.provider.business_name', read_only=True
    )
    service_name = serializers.CharField(source='provider_service.service.name', read_only=True)
    category_name = serializers.CharField(
        source='provider_service.service.category.name', read_only=True
    )
    is_payment_expired = serializers.BooleanField(read_only=True)

    class Meta:
        model = Booking
        fields = [
            'id', 'customer', 'provider_service',
            'provider', 'provider_business_name', 'service_name', 'category_name',
            'event_date', 'notes',
            'price_snapshot', 'commission_rate_snapshot',
            'status', 'accepted_at', 'payment_deadline', 'is_payment_expired',
            'created_at', 'updated_at',
        ]
        read_only_fields = [
            'id', 'customer', 'price_snapshot', 'commission_rate_snapshot',
            'status', 'accepted_at', 'payment_deadline', 'created_at', 'updated_at',
        ]
        extra_kwargs = {'notes': {'max_length': 2000}}

    def validate_event_date(self, value):
        today = timezone.localdate()
        if value < today:
            raise serializers.ValidationError("The event date cannot be in the past.")
        if value < today + timedelta(days=MIN_DAYS_AHEAD_TO_ACCEPT):
            raise serializers.ValidationError(
                f"The event date must be at least {MIN_DAYS_AHEAD_TO_ACCEPT} days from today, "
                "so the provider can accept and you can pay in time."
            )
        return value

    def create(self, validated_data):
        request = self.context['request']
        customer = request.user
        submitted = validated_data['provider_service']
        event_date = validated_data['event_date']

        # Lock order everywhere: Provider -> ProviderService -> Booking -> Payment.
        with transaction.atomic():
            provider = Provider.objects.select_for_update().get(pk=submitted.provider_id)
            provider_service = (
                ProviderService.objects.select_for_update(of=('self',))
                .select_related('service__category', 'provider__user')
                .get(pk=submitted.pk)
            )

            if not provider_service.is_bookable:
                raise serializers.ValidationError(
                    {'provider_service': 'This service is not available for booking.'}
                )
            if provider.user_id == customer.id:
                raise serializers.ValidationError(
                    {'provider_service': 'You cannot book your own service.'}
                )
            if Booking.objects.filter(
                customer=customer,
                provider_service__provider_id=provider.pk,
                event_date=event_date,
                status__in=ACTIVE_BOOKING_STATUSES,
            ).exists():
                raise serializers.ValidationError(
                    {'event_date': 'You already have an active booking with this provider on this date.'}
                )
            if Booking.objects.filter(
                provider_service__provider_id=provider.pk,
                event_date=event_date,
                status__in=DATE_BLOCKING_STATUSES,
            ).exists():
                raise Conflict('This provider is not available on that date.')

            return Booking.objects.create(
                customer=customer,
                provider_service=provider_service,
                event_date=event_date,
                notes=validated_data.get('notes', ''),
            )

    def to_representation(self, instance):
        data = super().to_representation(instance)
        request = self.context.get('request')
        user = getattr(request, 'user', None)
        allowed = bool(
            user
            and user.is_authenticated
            and (user.is_staff or instance.provider_service.provider.user_id == user.id)
        )
        if not allowed:
            data.pop('commission_rate_snapshot', None)
        return data


def _generate_unique_username(email):
    base = email.split('@')[0]
    username = base
    suffix = 1
    while User.objects.filter(username=username).exists():
        suffix += 1
        username = f"{base}{suffix}"
    return username


def _classify_integrity_error(exc):
    """Returns 'username', 'email', or None (unrecognized) based on the DB's own constraint name."""
    cause = getattr(exc, '__cause__', None)
    diag = getattr(cause, 'diag', None)
    constraint_name = getattr(diag, 'constraint_name', None)
    if constraint_name:
        if constraint_name == USERNAME_CONSTRAINT:
            return 'username'
        if constraint_name == EMAIL_CONSTRAINT:
            return 'email'
        return None  # a real, different constraint — don't guess
    # Fallback only if the driver didn't give us structured diagnostics at all
    msg = str(exc).lower()
    if 'username' in msg:
        return 'username'
    if 'email' in msg:
        return 'email'
    return None


class CustomerRegistrationSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, validators=[validate_password])

    def validate_email(self, value):
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("Unable to register with these details.")
        return value

    def create(self, validated_data):
        email = validated_data['email']
        password = validated_data['password']
        for _ in range(5):
            username = _generate_unique_username(email)
            try:
                with transaction.atomic():
                    user = User.objects.create_user(username=username, email=email, password=password)
                    EmailVerification.objects.create(user=user)
                    return user
            except IntegrityError as exc:
                kind = _classify_integrity_error(exc)
                if kind == 'username':
                    logger.warning("Registration username collision, retrying: %s", exc)
                    continue
                if kind == 'email':
                    logger.info("Registration email collision (race): %s", exc)
                    raise serializers.ValidationError({'email': "Unable to register with these details."})
                logger.error("Unrecognized IntegrityError during customer registration: %s", exc)
                raise
            except Exception as exc:
                logger.error("Unexpected error during customer registration: %s", exc)
                raise
        raise serializers.ValidationError("Registration failed, please try again.")


class ProviderRegistrationSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, validators=[validate_password])
    business_name = serializers.CharField(max_length=255)

    def validate_email(self, value):
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("Unable to register with these details.")
        return value

    def create(self, validated_data):
        email = validated_data['email']
        password = validated_data['password']
        business_name = validated_data['business_name']
        for _ in range(5):
            username = _generate_unique_username(email)
            try:
                with transaction.atomic():
                    user = User.objects.create_user(username=username, email=email, password=password)
                    Provider.objects.create(user=user, business_name=business_name)
                    EmailVerification.objects.create(user=user)                    
                    return user
            except IntegrityError as exc:
                kind = _classify_integrity_error(exc)
                if kind == 'username':
                    logger.warning("Registration username collision, retrying: %s", exc)
                    continue
                if kind == 'email':
                    logger.info("Registration email collision (race): %s", exc)
                    raise serializers.ValidationError({'email': "Unable to register with these details."})
                logger.error("Unrecognized IntegrityError during provider registration: %s", exc)
                raise
            except Exception as exc:
                logger.error("Unexpected error during provider registration: %s", exc)
                raise
        raise serializers.ValidationError("Registration failed, please try again.")


def _login_cache_keys(email):
    digest = hashlib.sha256(email.strip().lower().encode('utf-8')).hexdigest()
    slot_keys = [f'login_slot:{digest}:{n}' for n in range(LOGIN_MAX_FAILURES)]
    return slot_keys, f'login_lock:{digest}'


def _throttled(unlock_at):
    wait = max(1, math.ceil(unlock_at - time.time())) if unlock_at else 1
    return exceptions.Throttled(wait=wait)


def _reserve_login_attempt(slot_keys, lock_key):
    """
    Claims one of the email's attempt slots BEFORE the password is checked.
    cache.add() is atomic on every backend (including DatabaseCache, where the
    primary key rejects a duplicate), so parallel requests can never share a
    slot, and the request that finds every slot taken is rejected before any
    password work happens.
    """
    unlock_at = cache.get(lock_key)
    if unlock_at is not None:
        raise _throttled(unlock_at)

    claimed = any(cache.add(slot_key, 1, LOGIN_FAILURE_WINDOW_SECONDS) for slot_key in slot_keys)
    if not claimed:
        raise _throttled(cache.get(lock_key))

    if len(cache.get_many(slot_keys)) == len(slot_keys):
        cache.set(lock_key, time.time() + LOGIN_LOCK_SECONDS, LOGIN_LOCK_SECONDS)


class EmailTokenObtainPairSerializer(TokenObtainPairSerializer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields.pop(self.username_field, None)
        self.fields['email'] = serializers.EmailField(write_only=True)

    def validate(self, attrs):
        email = attrs['email']
        slot_keys, lock_key = _login_cache_keys(email)
        _reserve_login_attempt(slot_keys, lock_key)

        user = User.objects.filter(email__iexact=email).first()
        username = user.username if user is not None else NO_SUCH_USERNAME

        data = super().validate({'username': username, 'password': attrs['password']})

        cache.delete_many(slot_keys + [lock_key])
        return data

MESSAGEABLE_STATUSES = ('pending', 'accepted', 'paid')
MESSAGE_MAX_LENGTH = 2000
MAX_DB_INT = 2147483647


class BookingRefSerializer(serializers.Serializer):
    booking = serializers.IntegerField(min_value=1, max_value=MAX_DB_INT)


class MessageListQuerySerializer(BookingRefSerializer):
    after_id = serializers.IntegerField(required=False, min_value=0, max_value=MAX_DB_INT)
    ordering = serializers.ChoiceField(
        choices=['created_at', '-created_at'], required=False
    )

    def validate(self, attrs):
        if 'after_id' in attrs:
            if attrs.get('ordering', 'created_at') != 'created_at':
                raise serializers.ValidationError(
                    {'ordering': 'after_id requires ascending order (created_at).'}
                )
            attrs['ordering'] = 'created_at'
        else:
            attrs.setdefault('ordering', '-created_at')
        return attrs


class MessageSerializer(serializers.ModelSerializer):
    content = serializers.CharField(max_length=MESSAGE_MAX_LENGTH)
    is_mine = serializers.SerializerMethodField()
    sender_role = serializers.SerializerMethodField()

    class Meta:
        model = Message
        fields = ['id', 'booking', 'content', 'is_read', 'created_at', 'is_mine', 'sender_role']
        read_only_fields = ['id', 'booking', 'is_read', 'created_at']

    def get_is_mine(self, obj):
        request = self.context.get('request')
        return request is not None and obj.sender_id == request.user.id

    def get_sender_role(self, obj):
        return 'customer' if obj.sender_id == obj.booking.customer_id else 'provider'


class VerifyEmailSerializer(serializers.Serializer):
    token = serializers.CharField(max_length=2000)


class PasswordResetRequestSerializer(serializers.Serializer):
    email = serializers.EmailField(max_length=254)


class PasswordResetConfirmSerializer(serializers.Serializer):
    uid = serializers.CharField(max_length=32)
    token = serializers.CharField(max_length=128)
    new_password = serializers.CharField(max_length=128, write_only=True)        