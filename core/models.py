import uuid

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models.functions import Lower
from django.utils.text import slugify


class Category(models.Model):
    name = models.CharField(max_length=80, unique=True)
    slug = models.SlugField(max_length=90, unique=True, blank=True)
    description = models.CharField(max_length=240, blank=True)
    sort_order = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["sort_order", "name"]
        verbose_name_plural = "categories"

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class Product(models.Model):
    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=130, unique=True, blank=True)
    category = models.ForeignKey(
        Category, on_delete=models.PROTECT, related_name="products"
    )
    description = models.TextField(blank=True)
    price_ugx = models.PositiveIntegerField(validators=[MinValueValidator(0)])
    image_url = models.URLField(max_length=1000, blank=True)
    image_public_id = models.CharField(max_length=255, blank=True)
    is_available = models.BooleanField(default=True)
    is_featured = models.BooleanField(default=False)
    sort_order = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort_order", "name"]

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class Lodge(models.Model):
    name = models.CharField(max_length=120, unique=True)
    slug = models.SlugField(max_length=130, unique=True, blank=True)
    description = models.TextField(blank=True)
    capacity = models.PositiveSmallIntegerField(
        default=2, validators=[MinValueValidator(1), MaxValueValidator(50)]
    )
    min_nights = models.PositiveSmallIntegerField(
        default=1, validators=[MinValueValidator(1), MaxValueValidator(365)]
    )
    max_nights = models.PositiveSmallIntegerField(
        default=30, validators=[MinValueValidator(1), MaxValueValidator(365)]
    )
    min_advance_days = models.PositiveSmallIntegerField(
        default=0, validators=[MinValueValidator(0), MaxValueValidator(365)]
    )
    max_advance_days = models.PositiveSmallIntegerField(
        default=365, validators=[MinValueValidator(1), MaxValueValidator(365)]
    )
    checkin_weekdays = models.JSONField(default=list, blank=True)
    image_url = models.URLField(max_length=1000, blank=True)
    image_public_id = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    def sync_cover_image(self):
        primary = self.images.order_by("sort_order", "id").first()
        image_url = primary.image_url if primary else ""
        image_public_id = primary.image_public_id if primary else ""
        if self.image_url != image_url or self.image_public_id != image_public_id:
            type(self).objects.filter(pk=self.pk).update(
                image_url=image_url, image_public_id=image_public_id
            )
            self.image_url = image_url
            self.image_public_id = image_public_id

    def __str__(self):
        return self.name


class LodgeImage(models.Model):
    lodge = models.ForeignKey(
        Lodge, on_delete=models.CASCADE, related_name="images"
    )
    image_url = models.URLField(max_length=1000)
    image_public_id = models.CharField(max_length=255, blank=True)
    sort_order = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["sort_order", "id"]

    def __str__(self):
        return f"{self.lodge} image"


class LodgeBlockout(models.Model):
    lodge = models.ForeignKey(
        Lodge, on_delete=models.CASCADE, related_name="blockouts"
    )
    start_date = models.DateField()
    end_date = models.DateField()
    reason = models.CharField(max_length=160, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["start_date", "lodge__name"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(end_date__gt=models.F("start_date")),
                name="lodge_blockout_end_after_start",
            )
        ]

    def __str__(self):
        return f"{self.lodge} unavailable {self.start_date}–{self.end_date}"


class GuestCart(models.Model):
    token_digest = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class GuestCartItem(models.Model):
    cart = models.ForeignKey(GuestCart, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    quantity = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(99)]
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(quantity__gte=1, quantity__lte=99),
                name="guest_cart_quantity_1_to_99",
            ),
            models.UniqueConstraint(
                fields=("cart", "product"), name="unique_guest_cart_product"
            )
        ]


class Order(models.Model):
    class Status(models.TextChoices):
        NEW = "new", "New"
        ACCEPTED = "accepted", "Confirmed"
        PREPARING = "preparing", "Preparing"
        READY = "ready", "Ready"
        COMPLETED = "completed", "Delivered"
        CANCELLED = "cancelled", "Cancelled"

    class OrderType(models.TextChoices):
        DINE_IN = "dine_in", "Dine-in"
        LODGE = "lodge", "Lodge / cottage"

    class PaymentMethod(models.TextChoices):
        CASH = "cash", "Cash at resort"
        CARD_AT_COUNTER = "card_at_counter", "Card at counter"

    class PaymentStatus(models.TextChoices):
        UNPAID = "unpaid", "Unpaid"
        PARTIAL = "partial", "Partially paid"
        PAID = "paid", "Paid in full"

    number = models.CharField(max_length=20, unique=True, editable=False)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.NEW, db_index=True
    )
    order_type = models.CharField(max_length=12, choices=OrderType.choices)
    location = models.CharField(max_length=80)
    guest_name = models.CharField(max_length=120)
    phone = models.CharField(max_length=40)
    email = models.EmailField(blank=True)
    payment_method = models.CharField(
        max_length=24, choices=PaymentMethod.choices, default=PaymentMethod.CASH
    )
    payment_status = models.CharField(
        max_length=12, choices=PaymentStatus.choices, default=PaymentStatus.UNPAID
    )
    note = models.TextField(blank=True)
    cancellation_reason = models.TextField(blank=True)
    total_ugx = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        if not self.number:
            self.number = "KR-" + uuid.uuid4().hex[:8].upper()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.number


class OrderItem(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(
        Product, on_delete=models.SET_NULL, null=True, blank=True
    )
    product_name = models.CharField(max_length=120)
    unit_price_ugx = models.PositiveIntegerField()
    quantity = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(99)]
    )
    line_total_ugx = models.PositiveIntegerField()

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.quantity} × {self.product_name}"


class LodgeReservation(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending Review"
        CONFIRMED = "confirmed", "Confirmed"
        DECLINED = "declined", "Rejected"
        CANCELLED = "cancelled", "Cancelled"
        CHECKED_IN = "checked_in", "Checked In"
        COMPLETED = "completed", "Checked Out"

    class Source(models.TextChoices):
        WEBSITE = "website", "Website"
        WALK_IN = "walk_in", "Walk-in"
        PHONE = "phone", "Phone call"
        STAFF = "staff", "Admin / staff"

    reference = models.CharField(max_length=20, unique=True, editable=False)
    lodge = models.ForeignKey(
        Lodge, on_delete=models.PROTECT, related_name="reservations"
    )
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    guest_name = models.CharField(max_length=120)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=40)
    check_in = models.DateField()
    check_out = models.DateField()
    adults = models.PositiveSmallIntegerField(
        default=1, validators=[MinValueValidator(1), MaxValueValidator(50)]
    )
    children = models.PositiveSmallIntegerField(
        default=0, validators=[MinValueValidator(0), MaxValueValidator(50)]
    )
    total_due_ugx = models.PositiveIntegerField(default=0)
    note = models.TextField(blank=True)
    cancellation_reason = models.TextField(blank=True)
    occasion = models.CharField(max_length=100, blank=True)
    source = models.CharField(
        max_length=12, choices=Source.choices, default=Source.WEBSITE, db_index=True
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        if not self.reference:
            self.reference = "LR-" + uuid.uuid4().hex[:8].upper()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.reference} — {self.guest_name}"


class Payment(models.Model):
    class Method(models.TextChoices):
        CASH = "cash", "Cash"
        CARD_AT_COUNTER = "card_at_counter", "Card at counter"
        MOBILE_MONEY = "mobile_money", "Mobile money"
        BANK_TRANSFER = "bank_transfer", "Bank transfer"
        OTHER = "other", "Other"

    class Status(models.TextChoices):
        VERIFIED = "verified", "Verified"

    order = models.ForeignKey(
        Order,
        on_delete=models.PROTECT,
        related_name="payments",
        null=True,
        blank=True,
    )
    reservation = models.ForeignKey(
        LodgeReservation,
        on_delete=models.PROTECT,
        related_name="payments",
        null=True,
        blank=True,
    )
    amount_ugx = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    method = models.CharField(max_length=24, choices=Method.choices)
    transaction_reference = models.CharField(max_length=120)
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.VERIFIED
    )
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="verified_payments",
        null=True,
        blank=True,
    )
    verified_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-verified_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(order__isnull=False, reservation__isnull=True)
                    | models.Q(order__isnull=True, reservation__isnull=False)
                ),
                name="payment_has_exactly_one_target",
            ),
            models.CheckConstraint(
                condition=models.Q(amount_ugx__gt=0),
                name="payment_amount_must_be_positive",
            ),
            models.UniqueConstraint(
                Lower("transaction_reference"),
                name="unique_payment_transaction_reference_ci",
            ),
        ]

    def __str__(self):
        return self.transaction_reference


class PaymentReceipt(models.Model):
    payment = models.OneToOneField(
        Payment, on_delete=models.PROTECT, related_name="receipt"
    )
    number = models.CharField(max_length=32, unique=True)
    verification_token_digest = models.CharField(max_length=64, unique=True)
    total_due_ugx = models.PositiveIntegerField()
    paid_to_date_ugx = models.PositiveIntegerField()
    balance_ugx = models.PositiveIntegerField()
    pdf_data = models.BinaryField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return self.number


class StaffProfile(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="staff_profile",
    )
    must_change_password = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Staff profile for {self.user.get_username()}"


class StaffPushSubscription(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="push_subscriptions",
    )
    endpoint = models.URLField(max_length=2048, unique=True)
    p256dh = models.CharField(max_length=255)
    auth = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"Push subscription for {self.user.get_username()}"
