from rest_framework import serializers

from .models import Category, Lodge, LodgeReservation, Order, OrderItem, Product


class StaffLoginSerializer(serializers.Serializer):
    username = serializers.CharField()
    password = serializers.CharField(write_only=True, trim_whitespace=False)


class StaffCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = ("id", "name", "slug", "description", "sort_order", "is_active")


class StaffLodgeSerializer(serializers.ModelSerializer):
    class Meta:
        model = Lodge
        fields = (
            "id",
            "name",
            "slug",
            "description",
            "capacity",
            "image_url",
            "is_active",
        )


class StaffProductSerializer(serializers.ModelSerializer):
    category_id = serializers.PrimaryKeyRelatedField(
        source="category", queryset=Category.objects.all()
    )
    category_name = serializers.CharField(source="category.name", read_only=True)

    class Meta:
        model = Product
        fields = (
            "id",
            "name",
            "slug",
            "category_id",
            "category_name",
            "description",
            "price_ugx",
            "image_url",
            "is_available",
            "is_featured",
            "sort_order",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "slug", "category_name", "created_at", "updated_at")


class StaffOrderItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = OrderItem
        fields = (
            "id",
            "product_name",
            "unit_price_ugx",
            "quantity",
            "line_total_ugx",
        )


class StaffOrderSerializer(serializers.ModelSerializer):
    items = StaffOrderItemSerializer(many=True, read_only=True)

    class Meta:
        model = Order
        fields = (
            "id",
            "number",
            "status",
            "order_type",
            "location",
            "guest_name",
            "phone",
            "payment_method",
            "payment_status",
            "note",
            "total_ugx",
            "created_at",
            "items",
        )


class StaffOrderUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Order
        fields = ("status", "payment_status")


class StaffReservationSerializer(serializers.ModelSerializer):
    lodge_name = serializers.CharField(source="lodge.name", read_only=True)

    class Meta:
        model = LodgeReservation
        fields = (
            "id",
            "reference",
            "status",
            "lodge",
            "lodge_name",
            "guest_name",
            "email",
            "phone",
            "check_in",
            "check_out",
            "adults",
            "children",
            "note",
            "created_at",
        )
        read_only_fields = fields


class StaffReservationStatusSerializer(serializers.ModelSerializer):
    class Meta:
        model = LodgeReservation
        fields = ("status",)
