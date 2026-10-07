from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction, IntegrityError
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db.models.functions import Lower
from django.utils import timezone
from django.utils.text import slugify

# A booking must be at least this many days away to be accepted (and to stay PENDING):
# payment needs a date after today, and the 24h payment window must be honest.
MIN_DAYS_AHEAD_TO_ACCEPT = 2

class PaymentNotConfirmable(Exception):
    """
    Raised by Payment.mark_paid_and_confirm_booking() when a successful
    Paymob charge cannot be applied to this booking, for a genuine reason:
    wrong booking status, expired deadline, amount mismatch, or an
    already-refunded payment. The webhook handler should catch this
    specific exception type and trigger a refund via
    Payment.record_refunded_charge().

    Deliberately does NOT subclass ValidationError — if it did, a generic
    `except ValidationError` anywhere else in the codebase could catch it
    by accident and trigger an unwanted refund. This must only ever be
    caught explicitly, by name.

    A redelivered webhook for a payment that was already confirmed is NOT
    an error — that case returns normally as a no-op and never raises
    this at all.
    """
    pass


class Category(models.Model):
    name = models.CharField(max_length=100)
    slug = models.SlugField(max_length=110, unique=True, blank=True, editable=False)
    description = models.TextField(blank=True)
    icon = models.CharField(max_length=50, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']
        verbose_name_plural = 'Categories'
        constraints = [
            models.UniqueConstraint(Lower('name'), name='unique_category_name_ci')
        ]

    def save(self, *args, **kwargs):
        self.name = self.name.strip()
        if not self.slug:
            base_slug = slugify(self.name, allow_unicode=True) or 'category'
            candidate = base_slug
            counter = 2
            while Category.objects.filter(slug=candidate).exclude(pk=self.pk).exists():
                candidate = f"{base_slug}-{counter}"
                counter += 1
            self.slug = candidate
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class Service(models.Model):
    category = models.ForeignKey(
        Category,
        on_delete=models.PROTECT,
        related_name='services'
    )
    name = models.CharField(max_length=150)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['category__name', 'name']
        constraints = [
            models.UniqueConstraint(
                Lower('name'), 'category', name='unique_service_name_per_category_ci'
            )
        ]

    def save(self, *args, **kwargs):
        self.name = self.name.strip()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} ({self.category.name})"


class Provider(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='provider_profile'
    )
    business_name = models.CharField(max_length=150)
    bio = models.TextField(blank=True)
    commission_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal('10.00'),
        validators=[MinValueValidator(0), MaxValueValidator(100)]
    )
    is_verified = models.BooleanField(default=False, db_index=True)
    is_active = models.BooleanField(
        default=True,
        db_index=True,
        help_text="Owner-controlled self-pause. Independent of is_suspended."
    )
    is_suspended = models.BooleanField(
        default=False,
        db_index=True,
        help_text=(
            "Staff-only moderation flag (fraud, policy violation, etc). "
            "Independent of is_active — a provider cannot set or clear this "
            "themselves, even by toggling is_active. A suspended provider is "
            "never publicly visible or bookable, regardless of is_active."
        )
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(commission_rate__gte=0) & models.Q(commission_rate__lte=100),
                name='commission_rate_between_0_and_100'
            )
        ]

    @property
    def is_bookable(self):
        return (
            self.is_active
            and self.is_verified
            and not self.is_suspended
            and self.user.is_active
        )

    def save(self, *args, **kwargs):
        self.business_name = self.business_name.strip()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.business_name


class ProviderService(models.Model):
    provider = models.ForeignKey(
        Provider,
        on_delete=models.CASCADE,
        related_name='provider_services'
    )
    service = models.ForeignKey(
        Service,
        on_delete=models.PROTECT,
        related_name='provider_services'
    )
    price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal('0.01'))]
    )
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['provider', 'service'], name='unique_provider_service'),
            models.CheckConstraint(condition=models.Q(price__gt=0), name='provider_service_price_positive'),
        ]

    @property
    def is_bookable(self):
        return (
            self.is_active
            and self.service.is_active
            and self.service.category.is_active
            and self.provider.is_bookable
        )

    def __str__(self):
        return f"{self.provider.business_name} — {self.service.name} ({self.price} EGP)"


class Booking(models.Model):
    """
    LOCK ORDER RULE for any code touching both a Booking and its Payment
    (webhook handler, payout action, expiry job, refund flow): always lock
    Booking first, then Payment — see mark_cancelled_and_refunded() below
    and every method on Payment that touches both. Locking in a different
    order anywhere else risks a database deadlock between two concurrent
    requests locking in opposite order.
    """

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        ACCEPTED = 'accepted', 'Accepted'
        PAID = 'paid', 'Paid'
        DECLINED = 'declined', 'Declined'
        EXPIRED = 'expired', 'Expired'
        CANCELLED = 'cancelled', 'Cancelled'
        COMPLETED = 'completed', 'Completed'

    customer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='bookings'
    )
    provider_service = models.ForeignKey(
        ProviderService,
        on_delete=models.PROTECT,
        related_name='bookings'
    )
    event_date = models.DateField()
    notes = models.TextField(blank=True)

    price_snapshot = models.DecimalField(max_digits=10, decimal_places=2)
    commission_rate_snapshot = models.DecimalField(max_digits=5, decimal_places=2)

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    payment_deadline = models.DateTimeField(null=True, blank=True)
    deadline_reminder_sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status', 'payment_deadline']),
        ]
        constraints = [
            models.CheckConstraint(condition=models.Q(price_snapshot__gt=0), name='booking_price_snapshot_positive'),
            models.CheckConstraint(
                condition=models.Q(commission_rate_snapshot__gte=0) & models.Q(commission_rate_snapshot__lte=100),
                name='booking_commission_snapshot_between_0_and_100'
            ),
        ]

    def save(self, *args, **kwargs):
        if self._state.adding:
            self.price_snapshot = self.provider_service.price
            self.commission_rate_snapshot = self.provider_service.provider.commission_rate
        super().save(*args, **kwargs)

    def mark_accepted(self):
        with transaction.atomic():
            locked = Booking.objects.select_for_update().get(pk=self.pk)
            if locked.status != self.Status.PENDING:
                raise ValidationError(f"Cannot accept a booking with status '{locked.status}'.")
            if not locked.provider_service.is_bookable:
                raise ValidationError("This provider/service is no longer bookable.")
            if locked.event_date < timezone.localdate() + timedelta(days=MIN_DAYS_AHEAD_TO_ACCEPT):
                raise ValidationError(
                    "Cannot accept a booking whose event date is too close: it must be at least "
                    f"{MIN_DAYS_AHEAD_TO_ACCEPT} days away so the customer has time to pay."
                )
            locked.status = self.Status.ACCEPTED
            locked.accepted_at = timezone.now()
            locked.payment_deadline = locked.accepted_at + timedelta(hours=24)
            locked.save(update_fields=['status', 'accepted_at', 'payment_deadline', 'updated_at'])
        self.refresh_from_db()

    def mark_declined(self):
        with transaction.atomic():
            locked = Booking.objects.select_for_update().get(pk=self.pk)
            if locked.status != self.Status.PENDING:
                raise ValidationError(f"Cannot decline a booking with status '{locked.status}'.")
            locked.status = self.Status.DECLINED
            locked.save(update_fields=['status', 'updated_at'])
        self.refresh_from_db()

    def mark_cancelled(self):
        with transaction.atomic():
            locked = Booking.objects.select_for_update().get(pk=self.pk)
            if locked.status not in (self.Status.PENDING, self.Status.ACCEPTED):
                raise ValidationError(
                    f"Cannot cancel a booking with status '{locked.status}'. "
                    "Paid bookings must go through mark_cancelled_and_refunded()."
                )
            locked.status = self.Status.CANCELLED
            locked.save(update_fields=['status', 'updated_at'])
        self.refresh_from_db()

    def mark_cancelled_and_refunded(self, refund_reference):
        """
        Call Paymob's refund API FIRST, outside this method. Only call this
        method after Paymob confirms the refund succeeded, passing the
        reference/ID it returned. This method only records that a refund
        already happened — it does not perform the refund itself.
        This is the ONLY place Payment.refunded_at ever gets set — it
        means "a confirmed, legitimate payment was later refunded due to
        cancellation." Any other refunded charge (never confirmed in the
        first place) is recorded via Payment.record_refunded_charge()
        instead, which never touches this field.
        If this method raises or the process crashes after a successful
        Paymob refund call but before this completes, that is a real
        money/DB mismatch requiring manual reconciliation.
        """
        if not refund_reference:
            raise ValidationError(
                "refund_reference is required — call Paymob's refund API first "
                "and pass the reference it returns."
            )
        with transaction.atomic():
            locked = Booking.objects.select_for_update().get(pk=self.pk)
            if locked.status != self.Status.PAID:
                raise ValidationError(f"Cannot cancel-and-refund a booking with status '{locked.status}'.")
            payment = Payment.objects.select_for_update().get(booking=locked)
            if payment.payout_completed:
                raise ValidationError(
                    "Cannot auto-refund — payout has already been made to the provider. "
                    "This requires manual reconciliation."
                )
            if payment.refunded_at is not None:
                raise ValidationError("This payment has already been refunded.")
            payment.refunded_at = timezone.now()
            payment.refund_reference = refund_reference
            payment.save(update_fields=['refunded_at', 'refund_reference'])
            locked.status = self.Status.CANCELLED
            locked.save(update_fields=['status', 'updated_at'])
        self.refresh_from_db()

    def mark_completed(self):
        with transaction.atomic():
            locked = Booking.objects.select_for_update().get(pk=self.pk)
            if locked.status != self.Status.PAID:
                raise ValidationError(f"Cannot complete a booking with status '{locked.status}'.")
            if locked.event_date > timezone.localdate():
                raise ValidationError("Cannot complete a booking before its event date has occurred.")
            if RefundAttempt.objects.filter(
                payment__booking=locked,
                kind=RefundAttempt.Kind.CANCELLATION,
                status__in=[
                    RefundAttempt.Status.IN_FLIGHT,
                    RefundAttempt.Status.UNKNOWN,
                    RefundAttempt.Status.GATEWAY_REFUNDED,
                ],
            ).exists():
                raise ValidationError("A refund for this booking is in progress; it cannot be completed now.")
            locked.status = self.Status.COMPLETED
            locked.save(update_fields=['status', 'updated_at'])
        self.refresh_from_db()

    def mark_expired(self):
        with transaction.atomic():
            locked = Booking.objects.select_for_update().get(pk=self.pk)
            if locked.status != self.Status.ACCEPTED:
                raise ValidationError(f"Cannot expire a booking with status '{locked.status}'.")
            if not locked.is_payment_expired:
                raise ValidationError("Cannot expire a booking whose payment deadline hasn't passed yet.")
            locked.status = self.Status.EXPIRED
            locked.save(update_fields=['status', 'updated_at'])
        self.refresh_from_db()

    def mark_expired_unanswered(self, *, older_than, event_before):
        """
        PENDING -> EXPIRED for a request the provider never answered in time
        (created before `older_than`) or whose event date is too close to ever
        be paid (before `event_before`). Called only by the scheduled job; the
        staleness rule is re-checked under the lock. A PENDING booking can never
        have a Payment, so only the Booking row is locked.
        """
        with transaction.atomic():
            locked = Booking.objects.select_for_update().get(pk=self.pk)
            if locked.status != self.Status.PENDING:
                raise ValidationError(
                    f"Cannot expire an unanswered booking with status '{locked.status}'."
                )
            if not (locked.created_at < older_than or locked.event_date < event_before):
                raise ValidationError("This booking is not stale; it cannot be expired as unanswered.")
            locked.status = self.Status.EXPIRED
            locked.save(update_fields=['status', 'updated_at'])
        self.refresh_from_db()

    @property
    def is_payment_expired(self):
        return (
            self.status == self.Status.ACCEPTED
            and self.payment_deadline is not None
            and timezone.now() > self.payment_deadline
        )

    def __str__(self):
        return f"Booking #{self.pk} — {self.customer} → {self.provider_service}"


class Message(models.Model):
    booking = models.ForeignKey(
        Booking,
        on_delete=models.CASCADE,
        related_name='messages'
    )
    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='sent_messages'
    )
    content = models.TextField()
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']
        indexes = [
            models.Index(fields=['booking', 'created_at']),
        ]

    def save(self, *args, **kwargs):
        self.content = self.content.strip()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Message from {self.sender} on Booking #{self.booking_id}"


class Payment(models.Model):
    booking = models.OneToOneField(
        Booking,
        on_delete=models.PROTECT,
        related_name='payment'
    )
    amount = models.DecimalField(max_digits=10, decimal_places=2)

    paymob_transaction_id = models.CharField(max_length=100, unique=True, null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    payout_completed = models.BooleanField(default=False)
    payout_completed_at = models.DateTimeField(null=True, blank=True)
    payout_reference = models.CharField(max_length=150, blank=True)

    refunded_at = models.DateTimeField(null=True, blank=True)
    refund_reference = models.CharField(max_length=150, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(amount__gt=0), name='payment_amount_positive'),
            models.CheckConstraint(
                condition=models.Q(payout_completed=False) | models.Q(paid_at__isnull=False),
                name='payout_requires_paid'
            ),
            models.CheckConstraint(
                condition=models.Q(payout_completed=False) | models.Q(refunded_at__isnull=True),
                name='payout_blocked_if_refunded'
            ),
            models.CheckConstraint(
                condition=models.Q(refunded_at__isnull=True) | models.Q(paid_at__isnull=False),
                name='refund_requires_paid'
            ),
        ]

    def save(self, *args, **kwargs):
        if self._state.adding:
            self.amount = self.booking.price_snapshot
        super().save(*args, **kwargs)

    def mark_paid_and_confirm_booking(self, paymob_transaction_id, amount_paid):
        """
        The ONLY correct way for the Paymob webhook to record a successful
        charge that should confirm this booking's payment. Locks Booking
        then Payment, in one transaction.

        Order of checks matters:
        1. amount_paid must already be a Decimal — this is checked before
           anything else, outside the lock, so a caller passing an int/
           float (e.g. raw Paymob cents) fails loudly immediately instead
           of silently comparing wrong and refunding a valid payment.
        2. Idempotency check — if this exact transaction already confirmed
           this payment, return silently (no-op). This comes before any
           status check, so a harmless redelivered webhook is never
           mistaken for a genuine failure.
        3. Every check after that raises PaymentNotConfirmable specifically
           — the webhook handler must catch that exact exception type (not
           a broad except) and call record_refunded_charge() in response.
        """
        if not isinstance(amount_paid, Decimal):
            raise TypeError(
                "amount_paid must be a Decimal — convert Paymob's integer cents "
                "(Decimal(amount_cents) / 100) before calling this method."
            )
        with transaction.atomic():
            locked_booking = Booking.objects.select_for_update().get(pk=self.booking_id)
            locked_payment = Payment.objects.select_for_update().get(pk=self.pk)

            if locked_payment.paid_at is not None and locked_payment.paymob_transaction_id == paymob_transaction_id:
                return False # redelivered webhook for an already-confirmed payment — not an error

            if locked_payment.refunded_at is not None:
                raise PaymentNotConfirmable("This payment has already been refunded.")
            if locked_booking.status != Booking.Status.ACCEPTED:
                raise PaymentNotConfirmable(f"Cannot confirm payment on a booking with status '{locked_booking.status}'.")
            if locked_booking.is_payment_expired:
                raise PaymentNotConfirmable("Cannot confirm payment — payment deadline has already passed.")
            if amount_paid != locked_payment.amount:
                raise PaymentNotConfirmable(
                    f"Amount mismatch: expected {locked_payment.amount}, Paymob reported {amount_paid}."
                )

            locked_payment.paid_at = timezone.now()
            locked_payment.paymob_transaction_id = paymob_transaction_id
            locked_payment.save(update_fields=['paid_at', 'paymob_transaction_id'])
            locked_booking.status = Booking.Status.PAID
            locked_booking.save(update_fields=['status', 'updated_at'])
        self.refresh_from_db()
        return True

    def record_refunded_charge(self, paymob_transaction_id, amount, refund_reference):
        """
        Records ANY charge that was received via Paymob but did not (and
        never will) confirm this Payment's main booking — covers a late
        payment, a charge on a cancelled/declined/expired booking, an
        amount mismatch, or a genuine double payment after this Payment
        was already confirmed by a different transaction. Every one of
        these cases uses this single method and this single table, so a
        bad or stray charge can NEVER overwrite or block this Payment
        row's own paid_at/refunded_at — those two fields only ever reflect
        the one legitimate confirmed charge and its own later cancellation-
        refund, if any (see mark_cancelled_and_refunded on Booking).

        Idempotent by database constraint: paymob_transaction_id is unique
        on RefundedCharge, so a redelivered webhook or a retried call for
        a transaction already recorded here is caught as an IntegrityError
        and silently treated as a no-op rather than raising.
        """
        if not refund_reference:
            raise ValidationError("refund_reference is required — refund via Paymob first, then record it here.")
        try:
            with transaction.atomic():
                Booking.objects.select_for_update().get(pk=self.booking_id)  # lock order: Booking before Payment
                locked_payment = Payment.objects.select_for_update().get(pk=self.pk)
                RefundedCharge.objects.create(
                    payment=locked_payment,
                    paymob_transaction_id=paymob_transaction_id,
                    amount=amount,
                    refund_reference=refund_reference,
                )
        except IntegrityError:
            pass  # already recorded — redelivered webhook or retried call, safe no-op

    @property
    def is_paid(self):
        return self.paid_at is not None

    @property
    def commission_amount(self):
        rate = self.booking.commission_rate_snapshot
        raw = self.amount * rate / Decimal('100')
        return raw.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    @property
    def payout_amount(self):
        return self.amount - self.commission_amount

    def __str__(self):
        return f"Payment for Booking #{self.booking_id} — {self.amount} EGP"


class RefundedCharge(models.Model):
    """
    Audit record for any Paymob charge that was refunded without ever
    confirming its Payment's main booking — a late payment, a charge on a
    booking that was already cancelled/declined/expired, an amount
    mismatch, or a genuine double payment. Kept entirely separate from
    Payment so a bad or stray charge can never overwrite the one
    legitimate confirmed payment's data.
    """
    payment = models.ForeignKey(
        Payment,
        on_delete=models.PROTECT,
        related_name='refunded_charges'
    )
    paymob_transaction_id = models.CharField(max_length=100, unique=True)
    amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal('0.01'))]
    )
    refund_reference = models.CharField(max_length=150)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(amount__gt=0), name='refunded_charge_amount_positive'),
        ]

    def __str__(self):
        return f"Refunded charge for Payment #{self.payment_id} — txn {self.paymob_transaction_id}"

class PaymentAttempt(models.Model):
    """One Paymob checkout session (intention) for a Payment. Audit + webhook correlation."""
    payment = models.ForeignKey(Payment, on_delete=models.PROTECT, related_name='attempts')
    special_reference = models.CharField(max_length=64, unique=True)
    paymob_order_id = models.BigIntegerField(null=True, blank=True, unique=True)
    paymob_intention_id = models.CharField(max_length=100, blank=True)
    client_secret = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=['payment', 'created_at'])]

    def __str__(self):
        return f"PaymentAttempt #{self.pk} for Payment #{self.payment_id}"


class RefundAttempt(models.Model):
    """
    Durable record of ONE Paymob refund of ONE charge (unique per charge). Created before the
    gateway call so a crash/timeout/failure is never silent, and so concurrent clicks and
    webhook redeliveries cannot double-refund.
    """
    class Kind(models.TextChoices):
        CANCELLATION = 'cancellation', 'Cancellation of a paid booking'
        UNCONFIRMED = 'unconfirmed', 'Charge that could not confirm a booking'

    class Status(models.TextChoices):
        IN_FLIGHT = 'in_flight', 'In flight'
        UNKNOWN = 'unknown', 'Outcome unknown (needs retry/reconcile)'
        GATEWAY_REFUNDED = 'gateway_refunded', 'Refunded at Paymob, DB not finalized'
        DONE = 'done', 'Done'
        FAILED = 'failed', 'Rejected by Paymob'

    payment = models.ForeignKey(Payment, on_delete=models.PROTECT, related_name='refund_attempts')
    kind = models.CharField(max_length=20, choices=Kind.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.IN_FLIGHT, db_index=True)
    paymob_transaction_id = models.CharField(max_length=100, unique=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    refund_reference = models.CharField(max_length=150, blank=True)
    last_error = models.CharField(max_length=255, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+'
    )
    started_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(amount__gt=0), name='refund_attempt_amount_positive'),
        ]

    def __str__(self):
        return f"RefundAttempt #{self.pk} ({self.kind}, {self.status})"

class EmailVerification(models.Model):
    """One row per user. verified_at is NULL until the emailed link is used. Staff are exempt from the gate."""
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='email_verification'
    )
    verified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"EmailVerification for user #{self.user_id} ({'verified' if self.verified_at else 'pending'})"
