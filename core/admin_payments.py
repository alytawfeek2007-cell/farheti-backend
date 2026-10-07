from django import forms
from django.contrib import admin, messages
from django.contrib.admin.sites import NotRegistered
from django.http import HttpResponseRedirect

from .models import Booking, Payment, PaymentAttempt, RefundAttempt
from .payments import (
    PaymentFlowError, cancel_and_refund, mark_attempt_not_refunded, mark_payout_done,
    resolve_attempt_manually, retry_refund_attempt,
)

MAX_REFUND_BATCH = 3  # each row can wait on a network call


class _ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_manage_permission(self, request):
        return request.user.is_superuser

    def response_change(self, request, obj):
        # A failed money action must not be followed by Django's green "changed successfully".
        if getattr(request, '_money_action_failed', False):
            return HttpResponseRedirect(request.get_full_path())
        return super().response_change(request, obj)


def _run_batch(modeladmin, request, ids, func, label):
    if not request.user.is_superuser:
        modeladmin.message_user(request, 'Only the owner (superuser) may do this.', messages.ERROR)
        return
    if len(ids) > MAX_REFUND_BATCH:
        modeladmin.message_user(request, f'Select at most {MAX_REFUND_BATCH} rows per run.', messages.ERROR)
        return
    done, failures = [], []
    for pk in ids:
        try:
            func(pk)
            done.append(str(pk))
        except PaymentFlowError as exc:
            failures.append(f'#{pk}: {str(exc) or "not found"}')
    if done:
        modeladmin.message_user(request, f'{label} succeeded for ids: {", ".join(done)}.', messages.SUCCESS)
    if failures:
        modeladmin.message_user(request, f'{label} failed for ' + ' | '.join(failures), messages.ERROR)


@admin.action(description='Cancel and refund selected PAID bookings (full refund via Paymob)', permissions=['refund'])
def cancel_and_refund_selected(modeladmin, request, queryset):
    ids = list(queryset.values_list('pk', flat=True)[:MAX_REFUND_BATCH + 1])
    _run_batch(
        modeladmin, request, ids,
        lambda pk: cancel_and_refund(booking_id=pk, actor=request.user, role='staff'),
        'Cancel and refund',
    )


# Add the action to the existing BookingAdmin without rewriting it.
_booking_admin = admin.site._registry.get(Booking)
if _booking_admin is not None:
    _booking_admin.actions = [*(_booking_admin.actions or []), cancel_and_refund_selected]
    _booking_admin.has_refund_permission = lambda request: request.user.is_superuser


# ------------------------------------------------------------------ Payment (payout marking)
class PayoutForm(forms.ModelForm):
    confirm_transfer = forms.BooleanField(
        required=False, label="I have already transferred the payout amount to the provider's bank account"
    )

    class Meta:
        model = Payment
        fields = ['payout_reference']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['payout_reference'].required = True
        self.fields['payout_reference'].label = 'Bank transfer reference'

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get('confirm_transfer'):
            raise forms.ValidationError('Tick the confirmation box after making the transfer.')
        return cleaned


_PAYMENT_RO = [
    'booking', 'amount', 'paymob_transaction_id', 'paid_at', 'refunded_at', 'refund_reference',
    'payout_completed', 'payout_completed_at', 'commission_amount', 'payout_amount', 'created_at',
]

try:
    admin.site.unregister(Payment)
except NotRegistered:
    pass


@admin.register(Payment)
class PaymentAdmin(_ReadOnlyAdmin):
    form = PayoutForm
    list_display = ('booking_id', 'amount', 'paid_at', 'refunded_at', 'payout_completed', 'payout_completed_at')
    list_filter = ('payout_completed',)
    search_fields = ('paymob_transaction_id', 'payout_reference')
    ordering = ('-created_at',)

    def get_fields(self, request, obj=None):
        if obj is None or obj.payout_completed:
            return [*_PAYMENT_RO, 'payout_reference']
        return [*_PAYMENT_RO, 'payout_reference', 'confirm_transfer']

    def get_readonly_fields(self, request, obj=None):
        if obj is None or obj.payout_completed:
            return [*_PAYMENT_RO, 'payout_reference']
        return _PAYMENT_RO

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser and (obj is None or not obj.payout_completed)

    def save_model(self, request, obj, form, change):
        try:
            mark_payout_done(
                payment_id=obj.pk, reference=form.cleaned_data['payout_reference'], actor=request.user
            )
        except PaymentFlowError as exc:
            request._money_action_failed = True
            messages.error(request, f'Payout NOT marked: {str(exc) or "not found"}')
            return
        messages.success(request, 'Payout marked as done.')

    def log_change(self, request, obj, message):
        return None  # the service writes its own audit row only when the action really happened


# ------------------------------------------------------------------ RefundAttempt
class ManualRefundForm(forms.ModelForm):
    confirm_manual = forms.BooleanField(
        required=False, label='I refunded this charge by hand in the Paymob dashboard'
    )

    class Meta:
        model = RefundAttempt
        fields = ['refund_reference']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['refund_reference'].required = True
        self.fields['refund_reference'].label = 'Paymob refund transaction id'

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get('confirm_manual'):
            raise forms.ValidationError('Tick the confirmation box after refunding in Paymob.')
        return cleaned


_REFUND_RO = [
    'payment', 'kind', 'status', 'paymob_transaction_id', 'amount', 'last_error',
    'attempts', 'requested_by', 'started_at', 'created_at', 'updated_at',
]


@admin.action(description='Retry selected refunds via Paymob', permissions=['manage'])
def retry_selected_refunds(modeladmin, request, queryset):
    ids = list(queryset.values_list('pk', flat=True)[:MAX_REFUND_BATCH + 1])
    _run_batch(
        modeladmin, request, ids,
        lambda pk: retry_refund_attempt(attempt_id=pk, actor=request.user),
        'Retry refund',
    )


@admin.action(
    description='Mark as NOT refunded (only after checking the Paymob dashboard)', permissions=['manage']
)
def mark_not_refunded_selected(modeladmin, request, queryset):
    ids = list(queryset.values_list('pk', flat=True)[:MAX_REFUND_BATCH + 1])
    _run_batch(
        modeladmin, request, ids,
        lambda pk: mark_attempt_not_refunded(attempt_id=pk, actor=request.user),
        'Mark not refunded',
    )


@admin.register(RefundAttempt)
class RefundAttemptAdmin(_ReadOnlyAdmin):
    form = ManualRefundForm
    actions = [retry_selected_refunds, mark_not_refunded_selected]
    list_display = ('id', 'kind', 'status', 'payment_id', 'amount', 'attempts', 'updated_at')
    list_filter = ('status', 'kind')
    search_fields = ('paymob_transaction_id', 'refund_reference')
    ordering = ('-updated_at',)

    def get_fields(self, request, obj=None):
        if obj is None or obj.status == RefundAttempt.Status.DONE:
            return [*_REFUND_RO, 'refund_reference']
        return [*_REFUND_RO, 'refund_reference', 'confirm_manual']

    def get_readonly_fields(self, request, obj=None):
        if obj is None or obj.status == RefundAttempt.Status.DONE:
            return [*_REFUND_RO, 'refund_reference']
        return _REFUND_RO

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser and (obj is None or obj.status != RefundAttempt.Status.DONE)

    def save_model(self, request, obj, form, change):
        try:
            resolve_attempt_manually(
                attempt_id=obj.pk, reference=form.cleaned_data['refund_reference'], actor=request.user
            )
        except PaymentFlowError as exc:
            request._money_action_failed = True
            submitted = form.cleaned_data['refund_reference'].strip()
            saved = RefundAttempt.objects.filter(
                pk=obj.pk, status=RefundAttempt.Status.GATEWAY_REFUNDED, refund_reference=submitted
            ).exists()
            if saved:
                messages.warning(
                    request,
                    f'Refund reference saved, but the booking was NOT finalized: {exc} See "Last error" below.',
                )
            else:
                messages.error(request, f'Refund NOT recorded: {str(exc) or "not found"}')
            return
        messages.success(request, 'Refund recorded and booking finalized.')

    def log_change(self, request, obj, message):
        return None


@admin.register(PaymentAttempt)
class PaymentAttemptAdmin(_ReadOnlyAdmin):
    list_display = ('id', 'payment_id', 'special_reference', 'paymob_order_id', 'created_at')
    search_fields = ('special_reference', 'paymob_order_id')
    ordering = ('-created_at',)
    readonly_fields = (
        'payment', 'special_reference', 'paymob_order_id', 'paymob_intention_id', 'created_at',
    )
    exclude = ('client_secret',)

    def has_change_permission(self, request, obj=None):
        return False