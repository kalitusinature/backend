import hashlib

from rest_framework import serializers

from .models import (
    Category,
    GuestCart,
    GuestCartItem,
    Lodge,
    LodgeReservation,
    Order,
    OrderItem,
    Product,
)
from .reservation_rules import validate_lodge_stay


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


class GuestCartItemSerializer(serializers.ModelSerializer):
    product = serializers.SlugRelatedField(slug_field="slug", read_only=True)
    name = serializers.CharField(source="product.name", read_only=True)
    price = serializers.IntegerField(source="product.price_ugx", read_only=True)
    image = serializers.CharField(source="product.image_url", read_only=True)
    line_total_ugx = serializers.SerializerMethodField()

    class Meta:
        model = GuestCartItem
        fields = ("product", "name", "price", "image", "quantity", "line_total_ugx")

    def get_line_total_ugx(self, item):
        return item.product.price_ugx * item.quantity


class GuestCartSerializer(serializers.ModelSerializer):
    items = GuestCartItemSerializer(many=True, read_only=True)
    item_count = serializers.SerializerMethodField()
    subtotal_ugx = serializers.SerializerMethodField()

    class Meta:
        model = GuestCart
        fields = ("items", "item_count", "subtotal_ugx")

    def get_item_count(self, cart):
        return sum(item.quantity for item in cart.items.all())

    def get_subtotal_ugx(self, cart):
        return sum(
            item.product.price_ugx * item.quantity
            for item in cart.items.all()
        )


class GuestCartItemAddSerializer(serializers.Serializer):
    product = serializers.SlugRelatedField(
        slug_field="slug",
        queryset=Product.objects.filter(is_available=True, category__is_active=True),
    )
    quantity = serializers.IntegerField(min_value=1, max_value=99, default=1)


class GuestCartItemQuantitySerializer(serializers.Serializer):
    quantity = serializers.IntegerField(min_value=0, max_value=99)


class LodgeSerializer(serializers.ModelSerializer):
    images = serializers.SerializerMethodField()

    class Meta:
        model = Lodge
        fields = (
            "id",
            "name",
            "slug",
            "description",
            "capacity",
            "min_nights",
            "max_nights",
            "checkin_weekdays",
            "image_url",
            "images",
        )

    def get_images(self, lodge):
        prefetched = getattr(lodge, "_prefetched_objects_cache", {})
        if "images" in prefetched:
            image_items = list(lodge.images.all())
        else:
            image_items = list(lodge.images.order_by("sort_order", "id"))
        urls = [image.image_url for image in image_items if image.image_url]
        if not urls and lodge.image_url:
            urls = [lodge.image_url]
        return urls


class OrderLineInputSerializer(serializers.Serializer):
    product = serializers.SlugRelatedField(
        slug_field="slug",
        queryset=Product.objects.filter(is_available=True, category__is_active=True),
    )
    quantity = serializers.IntegerField(min_value=1, max_value=99)


class OrderCreateSerializer(serializers.Serializer):
    guest_name = serializers.CharField(max_length=120)
    phone = serializers.CharField(max_length=40)
    email = serializers.EmailField(required=False, allow_blank=True)
    order_type = serializers.ChoiceField(choices=Order.OrderType.choices)
    location = serializers.CharField(max_length=80)
    payment_method = serializers.ChoiceField(
        choices=Order.PaymentMethod.choices, default=Order.PaymentMethod.CASH
    )
    note = serializers.CharField(required=False, allow_blank=True, max_length=2000)
    items = OrderLineInputSerializer(many=True, allow_empty=False, required=False)
    cart_token = serializers.CharField(max_length=64, required=False, write_only=True)

    def validate(self, attrs):
        cart_token = attrs.get("cart_token")
        has_items = "items" in attrs
        if bool(cart_token) == has_items:
            raise serializers.ValidationError(
                {"items": "Provide either a cart token or an items list."}
            )
        if cart_token:
            cart = (
                GuestCart.objects.filter(
                    token_digest=hashlib.sha256(cart_token.encode("utf-8")).hexdigest()
                )
                .prefetch_related("items__product__category")
                .first()
            )
            if cart is None:
                raise serializers.ValidationError(
                    {"cart_token": "This cart is unavailable. Please refresh your cart."}
                )
            cart_items = list(cart.items.all())
            if not cart_items:
                raise serializers.ValidationError(
                    {"cart_token": "Your cart is empty."}
                )
            if any(
                not item.product.is_available or not item.product.category.is_active
                for item in cart_items
            ):
                raise serializers.ValidationError(
                    {"cart_token": "A cart item is no longer available."}
                )
            attrs["items"] = [
                {"product": item.product, "quantity": item.quantity}
                for item in cart_items
            ]
            attrs["_guest_cart"] = cart
            attrs.pop("cart_token")
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
    email = serializers.EmailField(max_length=254)
    phone = serializers.RegexField(
        regex=r"^\+?[0-9][0-9\s().-]{5,38}[0-9]$", max_length=40
    )
    check_in = serializers.DateField()
    check_out = serializers.DateField()
    adults = serializers.IntegerField(min_value=1, max_value=50, default=1)
    children = serializers.IntegerField(min_value=0, max_value=50, default=0)
    note = serializers.CharField(required=False, allow_blank=True, max_length=2000)
    occasion = serializers.CharField(required=False, allow_blank=True, max_length=100)

    def validate(self, attrs):
        validate_lodge_stay(
            lodge=attrs["lodge"],
            check_in=attrs["check_in"],
            check_out=attrs["check_out"],
            adults=attrs["adults"],
            children=attrs["children"],
            status=LodgeReservation.Status.PENDING,
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
    email = serializers.EmailField(max_length=254)


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
    status_label = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = LodgeReservation
        fields = (
            "reference",
            "status",
            "status_label",
            "lodge_name",
            "check_in",
            "check_out",
        )
