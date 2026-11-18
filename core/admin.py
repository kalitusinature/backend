from django.contrib import admin

from .models import Category, Lodge, LodgeReservation, Order, OrderItem, Product


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


@admin.register(Lodge)
class LodgeAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "capacity", "is_active")
    list_editable = ("capacity", "is_active")
    search_fields = ("name", "slug", "description")
    prepopulated_fields = {"slug": ("name",)}


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
    readonly_fields = ("number", "total_ugx", "created_at", "updated_at")
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
        "created_at",
    )
    list_filter = ("status", "lodge", "check_in")
    search_fields = ("reference", "guest_name", "phone", "email", "lodge__name")
    readonly_fields = ("reference", "created_at")
    date_hierarchy = "check_in"
