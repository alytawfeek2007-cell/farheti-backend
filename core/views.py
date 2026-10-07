import hashlib

from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.tokens import default_token_generator
from django.core.cache import cache
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken

from . import emails

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import Http404
from rest_framework import generics, mixins, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.generics import get_object_or_404
from rest_framework.permissions import AllowAny, BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView
from rest_framework.throttling import UserRateThrottle

from .models import Booking, Category, Provider, ProviderService, Service, Message, EmailVerification
from .serializers import (
    DATE_BLOCKING_STATUSES,
    PUBLIC_PROVIDER_SERVICE_Q,
    BookingSerializer,
    CategorySerializer,
    Conflict,
    CustomerRegistrationSerializer,
    EmailTokenObtainPairSerializer,
    ProviderRegistrationSerializer,
    ProviderSerializer,
    ProviderServiceSerializer,
    ProviderServiceUpdateSerializer,
    PublicProviderSerializer,
    ServiceSerializer,
    BookingRefSerializer, MESSAGEABLE_STATUSES,
    MessageListQuerySerializer, MessageSerializer,
    PasswordResetConfirmSerializer, PasswordResetRequestSerializer, 
    VerifyEmailSerializer, _login_cache_keys,    
)



class IsAdminOrReadOnly(permissions.BasePermission):
    def has_permission(self, request, view):
        if request.method in permissions.SAFE_METHODS:
            return True
        return bool(request.user and request.user.is_staff)


class IsOwnerOrStaffOrReadOnly(permissions.BasePermission):
    def has_object_permission(self, request, view, obj):
        if request.method in permissions.SAFE_METHODS:
            return True
        user = request.user
        return bool(user and user.is_authenticated and (user.is_staff or obj.user_id == user.id))

class IsEmailVerified(permissions.BasePermission):
    message = 'Please verify your email address first.'
    code = 'email_not_verified'

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        return user.is_staff or EmailVerification.objects.filter(user=user, verified_at__isnull=False).exists()

class CategoryViewSet(viewsets.ModelViewSet):
    serializer_class = CategorySerializer
    permission_classes = [IsAdminOrReadOnly]
    http_method_names = ['get', 'post', 'put', 'patch', 'head', 'options']

    def get_queryset(self):
        qs = Category.objects.all().order_by('name')
        is_staff_viewer = self.request.user.is_authenticated and self.request.user.is_staff
        if not is_staff_viewer:
            qs = qs.filter(is_active=True)
        return qs

    def perform_create(self, serializer):
        try:
            with transaction.atomic():
                serializer.save()
        except IntegrityError:
            raise DRFValidationError({"name": "A category with this name already exists."})

    def perform_update(self, serializer):
        try:
            with transaction.atomic():
                serializer.save()
        except IntegrityError:
            raise DRFValidationError({"name": "A category with this name already exists."})


class ServiceViewSet(viewsets.ModelViewSet):
    serializer_class = ServiceSerializer
    permission_classes = [IsAdminOrReadOnly]
    http_method_names = ['get', 'post', 'put', 'patch', 'head', 'options']

    def get_queryset(self):
        qs = Service.objects.select_related('category').order_by('category__name', 'name')
        is_staff_viewer = self.request.user.is_authenticated and self.request.user.is_staff
        if not is_staff_viewer:
            qs = qs.filter(is_active=True, category__is_active=True)
        return qs

    def perform_create(self, serializer):
        try:
            with transaction.atomic():
                serializer.save()
        except IntegrityError:
            raise DRFValidationError({"name": "A service with this name already exists in this category."})

    def perform_update(self, serializer):
        try:
            with transaction.atomic():
                serializer.save()
        except IntegrityError:
            raise DRFValidationError({"name": "A service with this name already exists in this category."})


class ProviderViewSet(viewsets.ModelViewSet):
    http_method_names = ['get', 'put', 'patch', 'head', 'options']

    def get_queryset(self):
        qs = Provider.objects.select_related('user').order_by('business_name', 'id')
        is_staff_viewer = self.request.user.is_authenticated and self.request.user.is_staff
        if not is_staff_viewer:
            qs = qs.filter(is_active=True, is_verified=True, is_suspended=False, user__is_active=True)
        return qs

    def get_object(self):
        obj = get_object_or_404(Provider.objects.select_related('user'), pk=self.kwargs['pk'])
        user = self.request.user
        is_owner = user.is_authenticated and obj.user_id == user.id
        is_staff = user.is_authenticated and user.is_staff
        is_public = obj.is_active and obj.is_verified and not obj.is_suspended and obj.user.is_active
        if not (is_owner or is_staff or is_public):
            raise Http404
        if self.request.method not in permissions.SAFE_METHODS:
            self.check_object_permissions(self.request, obj)
        self._is_privileged_viewer = is_owner or is_staff
        return obj

    def get_permissions(self):
        if self.action == 'me':
            return [permissions.IsAuthenticated()]
        if self.action in ('list', 'retrieve'):
            return [permissions.AllowAny()]
        return [permissions.IsAuthenticated(), IsOwnerOrStaffOrReadOnly()]

    def get_serializer_class(self):
        if self.action == 'list':
            is_staff_viewer = self.request.user.is_authenticated and self.request.user.is_staff
            return ProviderSerializer if is_staff_viewer else PublicProviderSerializer
        if self.action == 'retrieve':
            return ProviderSerializer if getattr(self, '_is_privileged_viewer', False) else PublicProviderSerializer
        return ProviderSerializer

    def perform_update(self, serializer):
        """
        serializer.instance is repointed at the freshly-locked row before
        saving, so the response (serializer.data, read after save()) shows
        the true current state of every field — not the possibly-stale
        object that get_object() fetched earlier, before this lock.
        """
        with transaction.atomic():
            locked = Provider.objects.select_for_update().get(pk=serializer.instance.pk)
            serializer.instance = locked
            new_business_name = serializer.validated_data.get('business_name')
            needs_reverify = (
                new_business_name is not None
                and new_business_name != locked.business_name
                and locked.is_verified
                and not self.request.user.is_staff
            )
            if needs_reverify:
                serializer.save(is_verified=False)
            else:
                serializer.save()

    @action(detail=False, methods=['get'])
    def me(self, request):
        try:
            provider = Provider.objects.select_related('user').get(user=request.user)
        except Provider.DoesNotExist:
            raise NotFound("No provider profile is associated with this account.")
        serializer = ProviderSerializer(provider, context=self.get_serializer_context())
        return Response(serializer.data)


class IsProviderUser(BasePermission):
    message = 'A provider account is required.'

    def has_permission(self, request, view):
        return getattr(request.user, 'provider_profile', None) is not None


class IsNotSuspendedProvider(BasePermission):
    message = 'Your provider account is suspended.'

    def has_permission(self, request, view):
        provider = getattr(request.user, 'provider_profile', None)
        return provider is not None and not provider.is_suspended


class IsListingOwnerOrStaff(BasePermission):
    def has_object_permission(self, request, view, obj):
        if request.user.is_staff:
            return True
        if obj.provider.user_id != request.user.id:
            return False
        if obj.provider.is_suspended:
            self.message = 'Your provider account is suspended.'
            return False
        return True


class ProviderServiceViewSet(viewsets.ModelViewSet):
    http_method_names = ['get', 'post', 'patch', 'head', 'options']
    ALLOWED_ORDERING = ('price', '-price', 'created_at', '-created_at')

    def get_serializer_class(self):
        if self.action == 'partial_update':
            return ProviderServiceUpdateSerializer
        return ProviderServiceSerializer

    def get_permissions(self):
        if self.action in ('list', 'retrieve', 'metadata'):
            permission_classes = [AllowAny]
        elif self.action == 'mine':
            permission_classes = [IsAuthenticated, IsProviderUser]
        elif self.action == 'create':
            permission_classes = [IsAuthenticated, IsProviderUser, IsNotSuspendedProvider, IsEmailVerified]
        elif self.action == 'partial_update':
            permission_classes = [IsAuthenticated, IsListingOwnerOrStaff]
        else:
            permission_classes = [IsAuthenticated]
        return [permission() for permission in permission_classes]

    def get_queryset(self):
        queryset = ProviderService.objects.select_related(
            'service__category', 'provider__user'
        )
        user = self.request.user
        if self.action == 'mine':
            return queryset.filter(provider__user=user)
        if user.is_staff:
            return queryset
        if self.action == 'list':
            return queryset.filter(PUBLIC_PROVIDER_SERVICE_Q)
        visible = PUBLIC_PROVIDER_SERVICE_Q
        if user.is_authenticated:
            visible = visible | Q(provider__user=user)
        return queryset.filter(visible)

    def _int_param(self, name):
        raw = self.request.query_params.get(name)
        if raw is None:
            return None
        try:
            value = int(raw)
        except ValueError:
            raise DRFValidationError({name: 'Must be an integer.'})
        if not 1 <= value <= 2147483647:
            raise DRFValidationError({name: 'Out of range.'})
        return value

    def filter_queryset(self, queryset):
        queryset = super().filter_queryset(queryset)
        if self.action not in ('list', 'mine'):
            return queryset
        for param, lookup in (
            ('provider', 'provider'),
            ('service', 'service'),
            ('category', 'service__category'),
        ):
            value = self._int_param(param)
            if value is not None:
                queryset = queryset.filter(**{lookup: value})
        ordering = self.request.query_params.get('ordering', '-created_at')
        if ordering not in self.ALLOWED_ORDERING:
            raise DRFValidationError({'ordering': 'Invalid ordering.'})
        return queryset.order_by(ordering, 'id')

    def perform_create(self, serializer):
        provider = self.request.user.provider_profile
        try:
            with transaction.atomic():
                serializer.save(provider=provider)
        except IntegrityError as exc:
            constraint = getattr(getattr(exc.__cause__, 'diag', None), 'constraint_name', None)
            if constraint == 'unique_provider_service' or (
                constraint is None and 'unique_provider_service' in str(exc)
            ):
                raise DRFValidationError({'service': 'You already offer this service.'})
            raise

    @action(detail=False, methods=['get'])
    def mine(self, request, *args, **kwargs):
        return self.list(request, *args, **kwargs)


class BookingViewSet(
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    """
    Bookings never change through PUT/PATCH/DELETE — only through the four
    action endpoints below, which call the model's self-locking mark_* methods.
    Lock order everywhere: Provider -> ProviderService -> Booking -> Payment.
    """
    serializer_class = BookingSerializer
    permission_classes = [IsAuthenticated]
    ALLOWED_ORDERING = ('created_at', '-created_at', 'event_date', '-event_date')

    def get_permissions(self):
        permission_list = [IsAuthenticated()]
        if self.action == 'create':
            permission_list.append(IsEmailVerified())
        return permission_list    

    def get_queryset(self):
        queryset = Booking.objects.select_related(
            'provider_service__service__category', 'provider_service__provider'
        )
        user = self.request.user
        if user.is_staff:
            return queryset
        return queryset.filter(Q(customer=user) | Q(provider_service__provider__user=user))

    def filter_queryset(self, queryset):
        queryset = super().filter_queryset(queryset)
        if self.action != 'list':
            return queryset
        params = self.request.query_params
        user = self.request.user

        role = params.get('role')
        if role is not None:
            if role == 'customer':
                queryset = queryset.filter(customer=user)
            elif role == 'provider':
                queryset = queryset.filter(provider_service__provider__user=user)
            else:
                raise DRFValidationError({'role': "Must be 'customer' or 'provider'."})

        status_filter = params.get('status')
        if status_filter is not None:
            if status_filter not in Booking.Status.values:
                raise DRFValidationError({'status': 'Invalid status.'})
            queryset = queryset.filter(status=status_filter)

        ordering = params.get('ordering', '-created_at')
        if ordering not in self.ALLOWED_ORDERING:
            raise DRFValidationError({'ordering': 'Invalid ordering.'})
        return queryset.order_by(ordering, 'id')

    def _role_of(self, booking):
        user = self.request.user
        if booking.customer_id == user.id:
            return 'customer'
        if booking.provider_service.provider.user_id == user.id:
            return 'provider'
        return None

    def _transition(self, booking, method_name, error_message=None):
        try:
            getattr(booking, method_name)()
        except DjangoValidationError as exc:
            raise Conflict(error_message or ' '.join(exc.messages))

    def _booking_response(self, pk):
        booking = self.get_queryset().get(pk=pk)
        return Response(self.get_serializer(booking).data)

    @action(detail=True, methods=['post'])
    def accept(self, request, pk=None):
        booking = self.get_object()
        if self._role_of(booking) != 'provider':
            raise PermissionDenied('Only the provider can accept a booking.')
        booking_pk = booking.pk
        provider_id = booking.provider_service.provider_id
        provider_service_id = booking.provider_service_id
        event_date = booking.event_date

        with transaction.atomic():
            # Lock order (same as BookingSerializer.create): Provider ->
            # ProviderService -> Booking. The Provider lock serialises accepts
            # and creations for this provider, so two bookings can never both be
            # confirmed for the same date; the ProviderService lock makes the
            # is_bookable check inside mark_accepted() see a stable listing.
            Provider.objects.select_for_update().get(pk=provider_id)
            ProviderService.objects.select_for_update().get(pk=provider_service_id)
            self._transition(booking, 'mark_accepted')
            if Booking.objects.filter(
                provider_service__provider_id=provider_id,
                event_date=event_date,
                status__in=DATE_BLOCKING_STATUSES,
            ).exclude(pk=booking_pk).exists():
                raise Conflict('You already have another confirmed booking on this date.')
            emails.notify_booking(booking_pk, 'accepted')
        return self._booking_response(booking_pk)

    @action(detail=True, methods=['post'])
    def decline(self, request, pk=None):
        booking = self.get_object()
        if self._role_of(booking) != 'provider':
            raise PermissionDenied('Only the provider can decline a booking.')
        self._transition(booking, 'mark_declined')
        emails.notify_booking(booking.pk, 'declined')        
        return self._booking_response(booking.pk)

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        booking = self.get_object()
        role = self._role_of(booking)
        if role is None:
            raise PermissionDenied('Only the customer or the provider can cancel a booking.')
        if booking.status == Booking.Status.PAID:
            raise Conflict('Paid bookings cannot be cancelled here. Please contact support.')
        if booking.status not in (Booking.Status.PENDING, Booking.Status.ACCEPTED):
            raise Conflict(f"Cannot cancel a booking with status '{booking.status}'.")
        if role == 'provider' and booking.status != Booking.Status.ACCEPTED:
            raise Conflict('Providers can only cancel accepted bookings; decline pending ones instead.')
        self._transition(booking, 'mark_cancelled', error_message='This booking can no longer be cancelled.')
        return self._booking_response(booking.pk)

    @action(detail=True, methods=['post'])
    def complete(self, request, pk=None):
        booking = self.get_object()
        if self._role_of(booking) is None:
            raise PermissionDenied('Only the customer or the provider can complete a booking.')
        self._transition(booking, 'mark_completed')
        return self._booking_response(booking.pk)


class CustomerRegisterView(generics.CreateAPIView):
    serializer_class = CustomerRegistrationSerializer
    permission_classes = [permissions.AllowAny]

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        emails.start_verification(user)        
        refresh = RefreshToken.for_user(user)
        return Response({
            'refresh': str(refresh),
            'access': str(refresh.access_token),
        }, status=status.HTTP_201_CREATED)


class ProviderRegisterView(generics.CreateAPIView):
    serializer_class = ProviderRegistrationSerializer
    permission_classes = [permissions.AllowAny]

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        emails.start_verification(user)        
        refresh = RefreshToken.for_user(user)
        return Response({
            'refresh': str(refresh),
            'access': str(refresh.access_token),
        }, status=status.HTTP_201_CREATED)


class EmailTokenObtainPairView(TokenObtainPairView):
    serializer_class = EmailTokenObtainPairSerializer
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'login'


class LogoutView(generics.GenericAPIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        try:
            refresh_token = request.data['refresh']
            token = RefreshToken(refresh_token)
            if str(token['user_id']) != str(request.user.id):
                return Response({'detail': 'Invalid or missing refresh token.'}, status=status.HTTP_400_BAD_REQUEST)
            token.blacklist()
        except Exception:
            return Response({'detail': 'Invalid or missing refresh token.'}, status=status.HTTP_400_BAD_REQUEST)
        return Response(status=status.HTTP_205_RESET_CONTENT)

class MessageSendThrottle(UserRateThrottle):
    scope = 'message_send'


class MessageViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    serializer_class = MessageSerializer
    permission_classes = [IsAuthenticated]
    http_method_names = ['get', 'post', 'head', 'options']
    ORDERINGS = {
        'created_at': ('created_at', 'id'),
        '-created_at': ('-created_at', '-id'),
    }

    def get_permissions(self):
        permission_list = [IsAuthenticated()]
        if self.action == 'create':
            permission_list.append(IsEmailVerified())
        return permission_list

    def get_throttles(self):
        throttles = super().get_throttles()
        if self.action == 'create':
            throttles.append(MessageSendThrottle())
        return throttles

    def _visible_bookings(self):
        user = self.request.user
        qs = Booking.objects.all()
        if user.is_staff:
            return qs
        return qs.filter(
            Q(customer=user) | Q(provider_service__provider__user=user)
        )

    def get_queryset(self):
        query = MessageListQuerySerializer(data=self.request.query_params)
        query.is_valid(raise_exception=True)
        params = query.validated_data
        booking = self._visible_bookings().filter(pk=params['booking']).first()
        if booking is None:
            raise NotFound()
        qs = Message.objects.filter(booking=booking).select_related('booking')
        if 'after_id' in params:
            qs = qs.filter(id__gt=params['after_id'])
        return qs.order_by(*self.ORDERINGS[params['ordering']])

    def create(self, request, *args, **kwargs):
        if request.user.is_staff:
            raise PermissionDenied('Staff accounts cannot send messages.')
        ref = BookingRefSerializer(data=request.data)
        ref.is_valid(raise_exception=True)
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            booking = (
                self._visible_bookings()
                .select_related('provider_service__provider__user')
                .select_for_update(of=('self',))
                .filter(pk=ref.validated_data['booking'])
                .first()
            )
            if booking is None:
                raise NotFound()
            if booking.status not in MESSAGEABLE_STATUSES:
                raise Conflict('Messaging is closed for this booking.')
            provider = booking.provider_service.provider
            if not provider.user.is_active:
                raise Conflict('This provider account is currently unavailable.')
            if request.user.id == provider.user_id and provider.is_suspended:
                raise PermissionDenied('Your provider account is suspended.')
            serializer.save(booking=booking, sender=request.user)

        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['post'], url_path='mark-read')
    def mark_read(self, request):
        if request.user.is_staff:
            raise PermissionDenied('Staff accounts cannot mark messages read.')
        ref = BookingRefSerializer(data=request.data)
        ref.is_valid(raise_exception=True)
        booking = self._visible_bookings().filter(pk=ref.validated_data['booking']).first()
        if booking is None:
            raise NotFound()
        updated = (
            Message.objects
            .filter(booking=booking, is_read=False)
            .exclude(sender=request.user)
            .update(is_read=True)
        )
        return Response({'marked_read': updated})        

INVALID_LINK = 'This link is invalid or has expired.'


class VerifyEmailView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'email_verify'

    def post(self, request):
        serializer = VerifyEmailSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = emails.read_verification_token(serializer.validated_data['token'])
        if user is None:
            return Response({'detail': INVALID_LINK}, status=status.HTTP_400_BAD_REQUEST)
        record, _ = EmailVerification.objects.get_or_create(user=user)
        if record.verified_at is None:
            EmailVerification.objects.filter(pk=record.pk, verified_at__isnull=True).update(
                verified_at=timezone.now()
            )
        return Response({'detail': 'Your email address is verified.'})


class ResendVerificationView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'email_resend'

    def post(self, request):
        user = request.user
        generic = {'detail': 'If your email is not verified yet, a new verification link has been sent.'}
        if user.is_staff or EmailVerification.objects.filter(user=user, verified_at__isnull=False).exists():
            return Response({'detail': 'Your email address is already verified.'})
        EmailVerification.objects.get_or_create(user=user)
        if cache.add(f'verify-resend:{user.pk}', 1, 60):
            emails.start_verification(user)
        return Response(generic)


class PasswordResetRequestView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'password_reset'

    def post(self, request):
        serializer = PasswordResetRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data['email']
        user = User.objects.filter(email__iexact=email, is_active=True).first()
        digest = hashlib.sha256(email.strip().lower().encode('utf-8')).hexdigest()
        # Same answer whether or not the account exists; one mail per address per minute.
        if user is not None and cache.add(f'pwreset:{digest}', 1, 60):
            emails.start_password_reset(user)
        return Response({'detail': 'If an account exists for this email, a password reset link has been sent.'})


class PasswordResetConfirmView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'password_reset_confirm'

    def post(self, request):
        serializer = PasswordResetConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        invalid = Response({'detail': INVALID_LINK}, status=status.HTTP_400_BAD_REQUEST)

        user = emails.user_from_reset_uid(data['uid'])
        if user is None or not default_token_generator.check_token(user, data['token']):
            return invalid
        try:
            validate_password(data['new_password'], user=user)
        except DjangoValidationError as exc:
            raise DRFValidationError({'new_password': list(exc.messages)})

        with transaction.atomic():
            locked = User.objects.select_for_update().get(pk=user.pk)
            # Re-check under the lock: a concurrent use of the same link changed the password hash.
            if not default_token_generator.check_token(locked, data['token']):
                return invalid
            locked.set_password(data['new_password'])
            locked.save(update_fields=['password'])
            for outstanding in OutstandingToken.objects.filter(user=locked):
                BlacklistedToken.objects.get_or_create(token=outstanding)

        slot_keys, lock_key = _login_cache_keys(locked.email)
        cache.delete_many(slot_keys + [lock_key])
        return Response({'detail': 'Your password has been updated.'})        