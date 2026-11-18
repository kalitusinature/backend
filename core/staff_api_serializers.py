import logging

import cloudinary
import cloudinary.uploader
from django.conf import settings
from django.db import transaction
from rest_framework import serializers

from .models import Category, Lodge, LodgeReservation, Order, OrderItem, Product

MAX_PRODUCT_IMAGE_BYTES = 1_572_864
PRODUCT_IMAGE_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}
logger = logging.getLogger(__name__)


def _configure_cloudinary():
    credentials = (
        settings.CLOUDINARY_NAME,
        settings.CLOUDINARY_KEY,
        settings.CLOUDINARY_SECRET,
    )
    if not all(credentials):
        return False
    cloudinary.config(
        cloud_name=settings.CLOUDINARY_NAME,
        api_key=settings.CLOUDINARY_KEY,
        api_secret=settings.CLOUDINARY_SECRET,
        secure=True,
    )
    return True


class ProductImageFileField(serializers.FileField):
    def __init__(self, **kwargs):
        kwargs.setdefault("allow_empty_file", False)
        super().__init__(**kwargs)

    def to_internal_value(self, data):
        image = super().to_internal_value(data)
        if image.size > MAX_PRODUCT_IMAGE_BYTES:
            raise serializers.ValidationError(
                "Images must be 1.5 MB or smaller after optimization."
            )
        if image.content_type not in PRODUCT_IMAGE_CONTENT_TYPES:
            raise serializers.ValidationError("Upload a PNG, JPEG, or WebP image.")
        return image


def _upload_product_image(image, public_id):
    if not _configure_cloudinary():
        raise serializers.ValidationError(
            {
                "image_file": (
                    "Set CLOUDINARY_NAME, CLOUDINARY_KEY, and CLOUDINARY_SECRET "
                    "on the backend to enable uploads."
                )
            }
        )

    try:
        result = cloudinary.uploader.upload(
            image,
            public_id=public_id,
            overwrite=True,
            invalidate=True,
            resource_type="image",
        )
    except cloudinary.exceptions.Error as error:
        raise serializers.ValidationError(
            {"image_file": "Cloudinary could not store this image. Try again."}
        ) from error

    if not result.get("secure_url") or not result.get("public_id"):
        raise serializers.ValidationError(
            {"image_file": "Cloudinary returned an incomplete upload result."}
        )
    return result


def _delete_cloudinary_image(public_id):
    if not _configure_cloudinary():
        logger.error(
            "Could not delete Cloudinary product image %s: credentials are not set.",
            public_id,
        )
        return

    try:
        result = cloudinary.uploader.destroy(
            public_id,
            invalidate=True,
            resource_type="image",
        )
    except cloudinary.exceptions.Error as error:
        logger.error(
            "Cloudinary image deletion failed for %s (%s).",
            public_id,
            type(error).__name__,
        )
        return

    if result.get("result") not in {"ok", "not found"}:
        logger.error(
            "Cloudinary did not delete product image %s (result: %s).",
            public_id,
            result.get("result", "unknown"),
        )


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
    image_file = ProductImageFileField(required=False, write_only=True)

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
            "image_file",
            "is_available",
            "is_featured",
            "sort_order",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "slug", "category_name", "created_at", "updated_at")

    @transaction.atomic
    def create(self, validated_data):
        image_file = validated_data.pop("image_file", None)
        product = super().create(validated_data)
        if image_file:
            result = _upload_product_image(
                image_file, f"kalitusi/products/{product.pk}"
            )
            product.image_url = result["secure_url"]
            product.image_public_id = result["public_id"]
            product.save(
                update_fields=("image_url", "image_public_id", "updated_at")
            )
        return product

    @transaction.atomic
    def update(self, instance, validated_data):
        image_file = validated_data.pop("image_file", None)
        old_public_id = instance.image_public_id

        if image_file:
            product = super().update(instance, validated_data)
            result = _upload_product_image(
                image_file, f"kalitusi/products/{product.pk}"
            )
            product.image_url = result["secure_url"]
            product.image_public_id = result["public_id"]
            product.save(
                update_fields=("image_url", "image_public_id", "updated_at")
            )
            if old_public_id and old_public_id != product.image_public_id:
                transaction.on_commit(
                    lambda: _delete_cloudinary_image(old_public_id)
                )
            return product

        image_url_changed = (
            "image_url" in validated_data
            and validated_data["image_url"] != instance.image_url
        )
        if image_url_changed and old_public_id:
            validated_data["image_public_id"] = ""

        product = super().update(instance, validated_data)
        if image_url_changed and old_public_id:
            transaction.on_commit(lambda: _delete_cloudinary_image(old_public_id))
        return product


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
            "email",
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

    def validate(self, attrs):
        order = self.instance
        if order is None:
            return attrs

        status_value = attrs.get("status", order.status)
        allowed_transitions = {
            Order.Status.NEW: {Order.Status.ACCEPTED, Order.Status.CANCELLED},
            Order.Status.ACCEPTED: {
                Order.Status.PREPARING,
                Order.Status.CANCELLED,
            },
            Order.Status.PREPARING: {Order.Status.READY, Order.Status.CANCELLED},
            Order.Status.READY: {Order.Status.COMPLETED, Order.Status.CANCELLED},
            Order.Status.COMPLETED: set(),
            Order.Status.CANCELLED: set(),
        }
        if (
            status_value != order.status
            and status_value not in allowed_transitions.get(order.status, set())
        ):
            raise serializers.ValidationError(
                {"status": "This order cannot move to the requested status."}
            )

        payment_status = attrs.get("payment_status", order.payment_status)
        if (
            status_value == Order.Status.CANCELLED
            and payment_status == Order.PaymentStatus.PAID
            and order.payment_status != Order.PaymentStatus.PAID
        ):
            raise serializers.ValidationError(
                {"payment_status": "A cancelled order cannot be marked paid."}
            )
        if (
            order.payment_status == Order.PaymentStatus.PAID
            and payment_status != Order.PaymentStatus.PAID
        ):
            raise serializers.ValidationError(
                {"payment_status": "A paid order cannot be marked unpaid."}
            )
        return attrs


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
