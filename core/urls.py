from django.urls import path
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenRefreshView

from .payment_views import (
    BookingCancelAndRefundView,
    BookingPayView,
    PaymentViewSet,
    PaymobWebhookView,
)
from .views import (
    BookingViewSet,
    CategoryViewSet,
    CustomerRegisterView,
    EmailTokenObtainPairView,
    LogoutView,
    MessageViewSet,
    ProviderRegisterView,
    ProviderServiceViewSet,
    ProviderViewSet,
    ServiceViewSet,
    PasswordResetConfirmView, 
    PasswordResetRequestView, 
    ResendVerificationView, 
    VerifyEmailView,
)

router = DefaultRouter()
router.register('categories', CategoryViewSet, basename='category')
router.register('services', ServiceViewSet, basename='service')
router.register('providers', ProviderViewSet, basename='provider')
router.register('provider-services', ProviderServiceViewSet, basename='provider-service')
router.register('bookings', BookingViewSet, basename='booking')
router.register('messages', MessageViewSet, basename='message')
router.register('payments', PaymentViewSet, basename='payment')

urlpatterns = router.urls + [
    path('auth/register/customer/', CustomerRegisterView.as_view(), name='register-customer'),
    path('auth/register/provider/', ProviderRegisterView.as_view(), name='register-provider'),
    path('auth/login/', EmailTokenObtainPairView.as_view(), name='login'),
    path('auth/logout/', LogoutView.as_view(), name='logout'),
    path('auth/refresh/', TokenRefreshView.as_view(), name='token-refresh'),
    path('auth/verify-email/', VerifyEmailView.as_view(), name='verify-email'),
    path('auth/resend-verification/', ResendVerificationView.as_view(), name='resend-verification'),
    path('auth/password-reset/', PasswordResetRequestView.as_view(), name='password-reset'),
    path('auth/password-reset/confirm/', PasswordResetConfirmView.as_view(), name='password-reset-confirm'),
    path('bookings/<int:pk>/pay/', BookingPayView.as_view(), name='booking-pay'),
    path('bookings/<int:pk>/cancel-and-refund/', BookingCancelAndRefundView.as_view(), name='booking-cancel-and-refund'),
    path('payments/paymob/webhook/', PaymobWebhookView.as_view(), name='paymob-webhook'),
]