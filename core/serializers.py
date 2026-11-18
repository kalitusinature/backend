from rest_framework import serializers

from .models import Category, Lodge, LodgeReservation, Order, OrderItem, Product


class CategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = ("id", "name", "slug", "description", "sort_order")


class ProductSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(source="category.name", read_only=True)
    category_slug = serializers.CharField(source="category.slug", read_only=True)
    price = serializers.IntegerField(source="price_ugx", read_only=True)
    image = serializers.URLField(source="image_url", read_only=True)

    class Meta:
        model = Product
        fields = (
            "id",
            "name",
            "slug",
            "category_name",
            "category_slug",
            "description",
            "price",
            "image",
            "is_featured",
        )


class LodgeSerializer(serializers.ModelSerializer):
    class Meta:
        model = Lodge
        fields = ("id", "name", "slug", "description", "capacity", "image_url")


class OrderLineInputSerializer(serializers.Serializer):
    product = serializers.SlugRelatedField(
        slug_field="slug",
        queryset=Product.objects.filter(is_available=True, category__is_active=True),
    )
    quantity = serializers.IntegerField(min_value=1, max_value=99)


class OrderCreateSerializer(serializers.Serializer):
    guest_name = serializers.CharField(max_length=120)
    phone = serializers.CharField(max_length=40)
    order_type = serializers.ChoiceField(choices=Order.OrderType.choices)
    location = serializers.CharField(max_length=80)
    payment_method = serializers.ChoiceField(
        choices=Order.PaymentMethod.choices, default=Order.PaymentMethod.CASH
    )
    note = serializers.CharField(required=False, allow_blank=True, max_length=2000)
    items = OrderLineInputSerializer(many=True, allow_empty=False)

    def validate(self, attrs):
        if attrs["order_type"] == Order.OrderType.DINE_IN and not attrs["location"].strip():
            raise serializers.ValidationError(
                {"location": "Enter the restaurant table number."}
            )
        if attrs["order_type"] == Order.OrderType.LODGE and not attrs["location"].strip():
            raise serializers.ValidationError(
                {"location": "Enter the lodge or cottage name/number."}
            )
        return attrs


class ReservationCreateSerializer(serializers.Serializer):
    lodge = serializers.SlugRelatedField(
        slug_field="slug", queryset=Lodge.objects.filter(is_active=True)
    )
    guest_name = serializers.CharField(max_length=120)
    email = serializers.EmailField(required=False, allow_blank=True)
    phone = serializers.CharField(max_length=40)
    check_in = serializers.DateField()
    check_out = serializers.DateField()
    adults = serializers.IntegerField(min_value=1, max_value=50, default=1)
    children = serializers.IntegerField(min_value=0, max_value=50, default=0)
    note = serializers.CharField(required=False, allow_blank=True, max_length=2000)

    def validate(self, attrs):
        if attrs["check_out"] <= attrs["check_in"]:
            raise serializers.ValidationError(
                {"check_out": "Check-out must be after check-in."}
            )
        if attrs["adults"] + attrs["children"] > attrs["lodge"].capacity:
            raise serializers.ValidationError(
                {"adults": "Guest count exceeds this lodge's capacity."}
            )
        overlaps = LodgeReservation.objects.filter(
            lodge=attrs["lodge"],
            status=LodgeReservation.Status.CONFIRMED,
            check_in__lt=attrs["check_out"],
            check_out__gt=attrs["check_in"],
        )
        if overlaps.exists():
            raise serializers.ValidationError(
                {"lodge": "This lodge is already reserved for those dates."}
            )
        return attrs

    def create(self, validated_data):
        return LodgeReservation.objects.create(**validated_data)


class OrderReceiptSerializer(serializers.ModelSerializer):
    class Meta:
        model = Order
        fields = ("number", "status", "total_ugx", "created_at")


class OrderTrackingRequestSerializer(serializers.Serializer):
    number = serializers.CharField(max_length=20)
    phone = serializers.CharField(max_length=40)


class OrderTrackingItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = OrderItem
        fields = ("product_name", "unit_price_ugx", "quantity", "line_total_ugx")


class OrderTrackingSerializer(serializers.ModelSerializer):
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    payment_status_label = serializers.CharField(
        source="get_payment_status_display", read_only=True
    )
    items = OrderTrackingItemSerializer(many=True, read_only=True)

    class Meta:
        model = Order
        fields = (
            "number",
            "status",
            "status_label",
            "order_type",
            "location",
            "payment_method",
            "payment_status",
            "payment_status_label",
            "total_ugx",
            "created_at",
            "updated_at",
            "items",
        )


class ReservationReceiptSerializer(serializers.ModelSerializer):
    lodge_name = serializers.CharField(source="lodge.name", read_only=True)

    class Meta:
        model = LodgeReservation
        fields = ("reference", "status", "lodge_name", "check_in", "check_out")
