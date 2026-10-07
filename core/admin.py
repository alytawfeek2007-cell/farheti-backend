from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.db import transaction


from .models import (
    Booking, Category, Message, Payment, Provider,
    ProviderService, RefundedCharge, Service,
)


class ReadOnlyMixin:
    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class SaveChangedFieldsMixin:
    """Write only the fields edited in the form, so an admin save can't
    overwrite a concurrent change to other columns made through the API."""

    def save_model(self, request, obj, form, change):
        if change:
            if form.changed_data:
                obj.save(update_fields=list(form.changed_data))
        else:
            super().save_model(request, obj, form, change)


@admin.register(Category)
class CategoryAdmin(SaveChangedFieldsMixin, admin.ModelAdmin):
    list_display = ('name', 'slug', 'is_active', 'created_at')
    list_filter = ('is_active',)
    search_fields = ('name',)
    readonly_fields = ('slug', 'created_at')

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Service)
class ServiceAdmin(SaveChangedFieldsMixin, admin.ModelAdmin):
    list_display = ('name', 'category', 'is_active', 'created_at')
    list_select_related = ('category',)
    list_filter = ('is_active', 'category')
    search_fields = ('name',)
    readonly_fields = ('created_at',)

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Provider)
class ProviderAdmin(SaveChangedFieldsMixin, admin.ModelAdmin):
    list_display = (
        'business_name', 'user', 'is_verified', 'is_active',
        'is_suspended', 'commission_rate', 'created_at',
    )
    list_select_related = ('user',)
    list_filter = ('is_verified', 'is_active', 'is_suspended')
    search_fields = ('business_name', 'user__username', 'user__email')
    actions = ['verify_providers', 'unverify_providers',
               'suspend_providers', 'unsuspend_providers']

    def get_readonly_fields(self, request, obj=None):
        readonly = ['user', 'business_name', 'bio', 'is_active', 'created_at']
        if not request.user.is_superuser:
            readonly.append('commission_rate')
        return readonly

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def _bulk_update(self, request, queryset, label, **fields):
        providers = list(queryset)
        changed = [
            p for p in providers
            if any(getattr(p, name) != value for name, value in fields.items())
        ]
        if changed:
            with transaction.atomic():
                Provider.objects.filter(pk__in=[p.pk for p in changed]).update(**fields)
                for provider in changed:
                    self.log_change(request, provider, f'Bulk action: {label}')
        message = f'{len(changed)} provider(s) {label}.'
        skipped = len(providers) - len(changed)
        if skipped:
            message += f' {skipped} already in that state.'
        self.message_user(
            request, message, messages.SUCCESS if changed else messages.INFO
        )
        return providers

    @admin.action(permissions=['change'], description='Verify selected providers')
    def verify_providers(self, request, queryset):
        self._bulk_update(request, queryset, 'verified', is_verified=True)

    @admin.action(permissions=['change'], description='Unverify selected providers')
    def unverify_providers(self, request, queryset):
        self._bulk_update(request, queryset, 'unverified', is_verified=False)

    @admin.action(permissions=['change'], description='Suspend selected providers')
    def suspend_providers(self, request, queryset):
        providers = self._bulk_update(request, queryset, 'suspended', is_suspended=True)
        if not providers:
            return
        open_bookings = Booking.objects.filter(
            provider_service__provider__in=[p.pk for p in providers],
            status__in=[
                Booking.Status.PENDING,
                Booking.Status.ACCEPTED,
                Booking.Status.PAID,
            ],
        ).count()
        if open_bookings:
            self.message_user(
                request,
                f'Warning: {open_bookings} open (pending/accepted/paid) booking(s) '
                'still exist for the selected provider(s). Review them in Bookings.',
                messages.WARNING,
            )

    @admin.action(permissions=['change'], description='Unsuspend selected providers')
    def unsuspend_providers(self, request, queryset):
        self._bulk_update(request, queryset, 'unsuspended', is_suspended=False)


@admin.register(ProviderService)
class ProviderServiceAdmin(SaveChangedFieldsMixin, admin.ModelAdmin):
    list_display = ('id', 'provider', 'service', 'price', 'is_active')
    list_select_related = ('provider', 'service__category')
    list_filter = ('is_active',)
    search_fields = ('provider__business_name', 'service__name')
    readonly_fields = ('provider', 'service', 'price', 'created_at')

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class MessageInline(ReadOnlyMixin, admin.TabularInline):
    model = Message
    extra = 0
    fields = ('sender', 'content', 'is_read', 'created_at')
    readonly_fields = fields
    verbose_name_plural = 'Chat messages'

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('sender')


class PaymentInline(ReadOnlyMixin, admin.StackedInline):
    model = Payment
    extra = 0
    fields = (
        'amount', 'paymob_transaction_id', 'paid_at', 'payout_completed',
        'payout_completed_at', 'payout_reference', 'refunded_at', 'refund_reference',
    )
    readonly_fields = fields


@admin.register(Booking)
class BookingAdmin(ReadOnlyMixin, admin.ModelAdmin):
    list_display = (
        'id', 'customer', 'provider_name', 'service_name',
        'event_date', 'status', 'price_snapshot', 'created_at',
    )
    list_select_related = (
        'customer', 'provider_service__provider', 'provider_service__service',
    )
    list_filter = ('status', 'event_date')
    search_fields = (
        'customer__username', 'customer__email',
        'provider_service__provider__business_name',
    )
    inlines = [PaymentInline, MessageInline]
    actions = ['cancel_bookings', 'decline_bookings',
               'expire_bookings', 'complete_bookings']

    @admin.display(description='Provider',
                   ordering='provider_service__provider__business_name')
    def provider_name(self, obj):
        return obj.provider_service.provider.business_name

    @admin.display(description='Service')
    def service_name(self, obj):
        return obj.provider_service.service.name

    # Actions use their own permission, because has_change_permission is False
    # (forms stay read-only) and would otherwise hide them.
    def has_manage_permission(self, request):
        return request.user.has_perm('core.change_booking')

    MAX_ACTION_BATCH = 200

    def _run(self, request, queryset, method_name, label):
        bookings = list(queryset[:self.MAX_ACTION_BATCH + 1])
        if len(bookings) > self.MAX_ACTION_BATCH:
            self.message_user(
                request,
                f'Select at most {self.MAX_ACTION_BATCH} bookings per action. '
                'Nothing was changed.',
                messages.ERROR,
            )
            return
        done = 0
        failures = []
        for booking in bookings:
            try:
                with transaction.atomic():
                    getattr(booking, method_name)()
                    self.log_change(request, booking, f'Admin action: {label}')
            except ValidationError as exc:
                failures.append(f"#{booking.pk}: {'; '.join(exc.messages)}")
                continue
            done += 1
        if done:
            self.message_user(request, f'{done} booking(s) {label}.', messages.SUCCESS)
        if failures:
            shown = '; '.join(failures[:10])
            extra = f' ... and {len(failures) - 10} more' if len(failures) > 10 else ''
            self.message_user(
                request, f'{len(failures)} booking(s) skipped. {shown}{extra}', messages.ERROR
            )

    @admin.action(permissions=['manage'], description='Cancel selected bookings (pending/accepted)')
    def cancel_bookings(self, request, queryset):
        self._run(request, queryset, 'mark_cancelled', 'cancelled')

    @admin.action(permissions=['manage'], description='Decline selected pending bookings')
    def decline_bookings(self, request, queryset):
        self._run(request, queryset, 'mark_declined', 'declined')

    @admin.action(permissions=['manage'], description='Expire selected overdue unpaid bookings')
    def expire_bookings(self, request, queryset):
        self._run(request, queryset, 'mark_expired', 'expired')

    @admin.action(permissions=['manage'], description='Mark selected paid bookings completed')
    def complete_bookings(self, request, queryset):
        self._run(request, queryset, 'mark_completed', 'completed')


@admin.register(Message)
class MessageAdmin(ReadOnlyMixin, admin.ModelAdmin):
    list_display = ('id', 'booking_id', 'sender', 'is_read', 'created_at')
    list_select_related = ('sender',)
    list_filter = ('is_read',)
    search_fields = ('sender__username', 'sender__email', 'content')


@admin.register(Payment)
class PaymentAdmin(ReadOnlyMixin, admin.ModelAdmin):
    list_display = ('id', 'booking_id', 'amount', 'paid_at',
                    'payout_completed', 'refunded_at', 'created_at')
    list_filter = ('payout_completed',)
    search_fields = ('paymob_transaction_id', 'payout_reference')
    fields = (
        'booking', 'amount', 'commission_amount', 'payout_amount',
        'paymob_transaction_id', 'paid_at', 'payout_completed',
        'payout_completed_at', 'payout_reference', 'refunded_at',
        'refund_reference', 'created_at',
    )
    readonly_fields = fields


@admin.register(RefundedCharge)
class RefundedChargeAdmin(ReadOnlyMixin, admin.ModelAdmin):
    list_display = ('id', 'payment_id', 'paymob_transaction_id',
                    'amount', 'refund_reference', 'created_at')
    search_fields = ('paymob_transaction_id', 'refund_reference')

from . import admin_payments  # noqa: F401    