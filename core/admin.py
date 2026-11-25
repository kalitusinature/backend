from django.contrib import admin

from .models import (
    Category,
    CustomerReview,
    Lodge,
    LodgeImage,
    LodgeReservation,
    Order,
    OrderItem,
    Payment,
    PaymentReceipt,
    Product,
    SupportTicket,
)


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "sort_order", "is_active")
    list_editable = ("sort_order", "is_active")
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "category",
        "price_ugx",
        "is_available",
        "is_featured",
        "sort_order",
    )
    list_filter = ("category", "is_available", "is_featured")
    list_editable = ("price_ugx", "is_available", "is_featured", "sort_order")
    search_fields = ("name", "slug", "description")
    prepopulated_fields = {"slug": ("name",)}
    autocomplete_fields = ("category",)


class LodgeImageInline(admin.TabularInline):
    model = LodgeImage
    extra = 1
    fields = ("image_url", "image_public_id", "sort_order")
    ordering = ("sort_order", "id")


@admin.register(Lodge)
class LodgeAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "capacity", "is_active", "image_count")
    list_editable = ("capacity", "is_active")
    search_fields = ("name", "slug", "description")
    prepopulated_fields = {"slug": ("name",)}
    inlines = (LodgeImageInline,)

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        return queryset.prefetch_related("images")

    @admin.display(description="Images")
    def image_count(self, obj):
        return obj.images.count()


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0
    readonly_fields = ("product_name", "unit_price_ugx", "quantity", "line_total_ugx")
    can_delete = False


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = (
        "number",
        "guest_name",
        "order_type",
        "location",
        "status",
        "total_ugx",
        "payment_status",
        "created_at",
    )
    list_filter = ("status", "order_type", "payment_method", "payment_status")
    search_fields = ("number", "guest_name", "phone", "location")
    readonly_fields = (
        "number",
        "total_ugx",
        "payment_status",
        "created_at",
        "updated_at",
    )
    inlines = (OrderItemInline,)
    date_hierarchy = "created_at"


@admin.register(LodgeReservation)
class LodgeReservationAdmin(admin.ModelAdmin):
    list_display = (
        "reference",
        "guest_name",
        "lodge",
        "check_in",
        "check_out",
        "adults",
        "children",
        "status",
        "total_due_ugx",
        "created_at",
    )
    list_filter = ("status", "lodge", "check_in")
    search_fields = ("reference", "guest_name", "phone", "email", "lodge__name")
    readonly_fields = ("reference", "total_due_ugx", "created_at")
    date_hierarchy = "check_in"


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = (
        "transaction_reference",
        "order",
        "reservation",
        "amount_ugx",
        "method",
        "verified_at",
    )
    list_filter = ("method", "status", "verified_at")
    search_fields = (
        "transaction_reference",
        "order__number",
        "reservation__reference",
    )
    readonly_fields = tuple(field.name for field in Payment._meta.fields)
    date_hierarchy = "verified_at"

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PaymentReceipt)
class PaymentReceiptAdmin(admin.ModelAdmin):
    list_display = ("number", "payment", "created_at")
    search_fields = ("number", "payment__transaction_reference")
    readonly_fields = (
        "payment",
        "number",
        "verification_token_digest",
        "total_due_ugx",
        "paid_to_date_ugx",
        "balance_ugx",
        "created_at",
    )
    exclude = ("pdf_data",)
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(CustomerReview)
class CustomerReviewAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "reviewer_name",
        "rating",
        "product",
        "lodge",
        "created_at",
    )
    list_filter = ("rating", "created_at")
    search_fields = ("reviewer_name", "comment", "product__name", "lodge__name")
    readonly_fields = tuple(field.name for field in CustomerReview._meta.fields)
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False


@admin.register(SupportTicket)
class SupportTicketAdmin(admin.ModelAdmin):
    list_display = ("id", "subject", "name", "email", "email_sent", "created_at")
    list_filter = ("email_sent", "created_at")
    search_fields = ("name", "email", "phone", "subject", "message")
    readonly_fields = tuple(field.name for field in SupportTicket._meta.fields)
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False
