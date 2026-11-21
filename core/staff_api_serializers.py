import logging
import uuid
from urllib.parse import urlsplit

import cloudinary
import cloudinary.uploader
from django.conf import settings
from django.contrib.auth import get_user_model, password_validation
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from rest_framework import serializers

from .models import (
    Category,
    Lodge,
    LodgeBlockout,
    LodgeImage,
    LodgeReservation,
    Order,
    OrderItem,
    Product,
)
from .reservation_rules import validate_lodge_stay

MAX_IMAGE_BYTES = 1_572_864
IMAGE_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}
logger = logging.getLogger(__name__)
User = get_user_model()


def _new_image_public_id(resource: str) -> str:
    return f"kalitusi/{resource}/{uuid.uuid4()}"


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


class ImageFileField(serializers.FileField):
    def __init__(self, **kwargs):
        kwargs.setdefault("allow_empty_file", False)
        super().__init__(**kwargs)

    def to_internal_value(self, data):
        image = super().to_internal_value(data)
        if image.size > MAX_IMAGE_BYTES:
            raise serializers.ValidationError(
                "Images must be 1.5 MB or smaller after optimization."
            )
        if image.content_type not in IMAGE_CONTENT_TYPES:
            raise serializers.ValidationError("Upload a PNG, JPEG, or WebP image.")
        return image


def _upload_cloudinary_image(image, public_id):
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
            overwrite=False,
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
            "Could not delete Cloudinary image %s: credentials are not set.",
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
            "Cloudinary did not delete image %s (result: %s).",
            public_id,
            result.get("result", "unknown"),
        )


class StaffLoginSerializer(serializers.Serializer):
    username = serializers.CharField()
    password = serializers.CharField(write_only=True, trim_whitespace=False)


class StaffAccountSerializer(serializers.ModelSerializer):
    must_change_password = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = (
            "username",
            "email",
            "first_name",
            "last_name",
            "is_superuser",
            "must_change_password",
        )
        read_only_fields = ("username", "is_superuser", "must_change_password")

    def get_must_change_password(self, user):
        profile = getattr(user, "staff_profile", None)
        return bool(profile and profile.must_change_password)


class StaffUserSerializer(serializers.ModelSerializer):
    must_change_password = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = (
            "id",
            "username",
            "email",
            "first_name",
            "last_name",
            "is_active",
            "is_superuser",
            "date_joined",
            "last_login",
            "must_change_password",
        )
        read_only_fields = fields

    def get_must_change_password(self, user):
        profile = getattr(user, "staff_profile", None)
        return bool(profile and profile.must_change_password)


class StaffCreateSerializer(serializers.Serializer):
    username = serializers.CharField(max_length=150)
    email = serializers.EmailField(required=False, allow_blank=True)
    first_name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    last_name = serializers.CharField(max_length=150, required=False, allow_blank=True)

    def validate_username(self, value):
        if User._default_manager.filter(username=value).exists():
            raise serializers.ValidationError("A user with this username already exists.")
        return value


class StaffCurrentPasswordSerializer(serializers.Serializer):
    current_password = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate_current_password(self, value):
        if not self.context["request"].user.check_password(value):
            raise serializers.ValidationError("The current password is incorrect.")
        return value


class StaffAccountPasswordSerializer(StaffCurrentPasswordSerializer):
    new_password = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        user = self.context["request"].user
        if attrs["current_password"] == attrs["new_password"]:
            raise serializers.ValidationError(
                {"new_password": "Choose a password different from your current one."}
            )
        try:
            password_validation.validate_password(attrs["new_password"], user=user)
        except DjangoValidationError as error:
            raise serializers.ValidationError(
                {"new_password": error.messages}
            ) from error
        return attrs


class StaffStatusSerializer(serializers.Serializer):
    is_active = serializers.BooleanField()


class StaffCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = ("id", "name", "slug", "description", "sort_order", "is_active")


class LodgeImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = LodgeImage
        fields = ("id", "image_url", "sort_order")
        read_only_fields = fields


class StaffLodgeSerializer(serializers.ModelSerializer):
    image_file = ImageFileField(required=False, write_only=True)
    image_files = serializers.ListField(
        child=ImageFileField(), required=False, allow_empty=True, write_only=True
    )
    remove_image_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        required=False,
        allow_empty=True,
        write_only=True,
    )
    images = LodgeImageSerializer(many=True, read_only=True)
    checkin_weekdays = serializers.ListField(
        child=serializers.IntegerField(min_value=0, max_value=6),
        required=False,
        allow_empty=True,
    )

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
            "min_advance_days",
            "max_advance_days",
            "checkin_weekdays",
            "image_url",
            "image_file",
            "image_files",
            "remove_image_ids",
            "images",
            "is_active",
        )
        read_only_fields = ("id", "slug", "images")

    def validate(self, attrs):
        instance = self.instance
        min_nights = attrs.get("min_nights", instance.min_nights if instance else 1)
        max_nights = attrs.get("max_nights", instance.max_nights if instance else 30)
        min_advance = attrs.get(
            "min_advance_days", instance.min_advance_days if instance else 0
        )
        max_advance = attrs.get(
            "max_advance_days", instance.max_advance_days if instance else 365
        )
        weekdays = attrs.get(
            "checkin_weekdays", instance.checkin_weekdays if instance else []
        )
        errors = {}
        if max_nights < min_nights:
            errors["max_nights"] = "Maximum stay must be at least the minimum stay."
        if max_advance < min_advance:
            errors["max_advance_days"] = (
                "Maximum booking notice must be at least the minimum notice."
            )
        if len(weekdays) != len(set(weekdays)):
            errors["checkin_weekdays"] = "Each check-in weekday may only be selected once."
        remove_ids = attrs.get("remove_image_ids") or []
        if remove_ids and len(remove_ids) != len(set(remove_ids)):
            errors["remove_image_ids"] = "Duplicate image IDs are not allowed."
        if instance and remove_ids:
            valid_ids = set(instance.images.values_list("id", flat=True))
            invalid = [image_id for image_id in remove_ids if image_id not in valid_ids]
            if invalid:
                errors["remove_image_ids"] = "One or more images do not belong to this lodge."
        if errors:
            raise serializers.ValidationError(errors)
        return attrs

    def _extract_uploads(self, validated_data):
        uploads = []
        single = validated_data.pop("image_file", None)
        if single:
            uploads.append(single)
        uploads.extend(validated_data.pop("image_files", None) or [])
        return uploads

    def _create_uploaded_images(self, lodge, uploads):
        if not uploads:
            return
        existing_max = (
            lodge.images.order_by("-sort_order", "-id")
            .values_list("sort_order", flat=True)
            .first()
        )
        next_order = (existing_max or 0) + 1
        for index, image_file in enumerate(uploads):
            result = _upload_cloudinary_image(
                image_file, _new_image_public_id("lodges")
            )
            LodgeImage.objects.create(
                lodge=lodge,
                image_url=result["secure_url"],
                image_public_id=result.get("public_id", ""),
                sort_order=next_order + index,
            )

    def _remove_images(self, lodge, remove_ids):
        if not remove_ids:
            return
        images = list(lodge.images.filter(id__in=remove_ids))
        lodge.images.filter(id__in=[image.id for image in images]).delete()
        for image in images:
            if image.image_public_id:
                public_id = image.image_public_id
                transaction.on_commit(lambda public_id=public_id: _delete_cloudinary_image(public_id))

    @transaction.atomic
    def create(self, validated_data):
        uploads = self._extract_uploads(validated_data)
        validated_data.pop("remove_image_ids", None)
        lodge = super().create(validated_data)
        self._create_uploaded_images(lodge, uploads)
        lodge.sync_cover_image()
        return lodge

    @transaction.atomic
    def update(self, instance, validated_data):
        uploads = self._extract_uploads(validated_data)
        remove_ids = validated_data.pop("remove_image_ids", None) or []
        old_public_id = instance.image_public_id

        # Ignore direct cover URL edits when managed image records are involved.
        if uploads or remove_ids or instance.images.exists():
            validated_data.pop("image_url", None)

        image_url_changed = (
            "image_url" in validated_data
            and validated_data["image_url"] != instance.image_url
        )
        if image_url_changed and old_public_id:
            validated_data["image_public_id"] = ""

        lodge = super().update(instance, validated_data)

        if remove_ids:
            self._remove_images(lodge, remove_ids)
        if uploads:
            self._create_uploaded_images(lodge, uploads)
        if uploads or remove_ids or lodge.images.exists():
            lodge.sync_cover_image()

        if image_url_changed and old_public_id and not lodge.images.exists():
            transaction.on_commit(lambda: _delete_cloudinary_image(old_public_id))
        return lodge


class StaffLodgeBlockoutSerializer(serializers.ModelSerializer):
    lodge_name = serializers.CharField(source="lodge.name", read_only=True)

    class Meta:
        model = LodgeBlockout
        fields = (
            "id",
            "lodge",
            "lodge_name",
            "start_date",
            "end_date",
            "reason",
            "created_at",
        )
        read_only_fields = ("id", "lodge_name", "created_at")

    def validate(self, attrs):
        instance = self.instance
        lodge = attrs.get("lodge", instance.lodge if instance else None)
        start_date = attrs.get(
            "start_date", instance.start_date if instance else None
        )
        end_date = attrs.get("end_date", instance.end_date if instance else None)
        errors = {}
        if start_date and end_date and end_date <= start_date:
            errors["end_date"] = "End date must be after start date."
        if lodge and start_date and end_date and end_date > start_date:
            occupied = LodgeReservation.objects.filter(
                lodge=lodge,
                status__in=(
                    LodgeReservation.Status.PENDING,
                    LodgeReservation.Status.CONFIRMED,
                ),
                check_in__lt=end_date,
                check_out__gt=start_date,
            ).exists()
            if occupied:
                errors["start_date"] = (
                    "This period overlaps an active reservation. Resolve it first."
                )
        if errors:
            raise serializers.ValidationError(errors)
        return attrs


class StaffProductSerializer(serializers.ModelSerializer):
    category_id = serializers.PrimaryKeyRelatedField(
        source="category", queryset=Category.objects.all()
    )
    category_name = serializers.CharField(source="category.name", read_only=True)
    image_file = ImageFileField(required=False, write_only=True)

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
            result = _upload_cloudinary_image(
                image_file, _new_image_public_id("products")
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
            result = _upload_cloudinary_image(
                image_file, _new_image_public_id("products")
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
    product_image_url = serializers.CharField(
        source="product.image_url",
        read_only=True,
        default="",
    )

    class Meta:
        model = OrderItem
        fields = (
            "id",
            "product_name",
            "product_image_url",
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
            "cancellation_reason",
            "total_ugx",
            "created_at",
            "items",
        )


class StaffOrderUpdateSerializer(serializers.ModelSerializer):
    cancellation_reason = serializers.CharField(
        required=False, allow_blank=True, max_length=1000
    )

    class Meta:
        model = Order
        fields = ("status", "payment_status", "cancellation_reason")

    def validate(self, attrs):
        order = self.instance
        if order is None:
            return attrs

        status_value = attrs.get("status", order.status)
        if (
            status_value == Order.Status.CANCELLED
            and order.status != Order.Status.CANCELLED
            and not attrs.get("cancellation_reason", order.cancellation_reason).strip()
        ):
            raise serializers.ValidationError(
                {"cancellation_reason": "A reason is required to cancel the order."}
            )
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
            "cancellation_reason",
            "occasion",
            "source",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


class StaffReservationCreateSerializer(serializers.ModelSerializer):
    lodge = serializers.PrimaryKeyRelatedField(queryset=Lodge.objects.all())
    lodge_name = serializers.CharField(source="lodge.name", read_only=True)
    phone = serializers.RegexField(
        regex=r"^\+?[0-9][0-9\s().-]{5,38}[0-9]$", max_length=40
    )

    class Meta:
        model = LodgeReservation
        fields = (
            "id",
            "reference",
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
            "occasion",
            "source",
            "status",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "reference", "lodge_name", "created_at", "updated_at")
        extra_kwargs = {
            "email": {"required": False, "allow_blank": True},
            "adults": {"required": False},
            "children": {"required": False},
            "note": {"required": False},
            "occasion": {"required": False},
            "source": {"required": False, "default": LodgeReservation.Source.STAFF},
            "status": {"required": False, "default": LodgeReservation.Status.PENDING},
        }

    def validate(self, attrs):
        status_value = attrs.get("status", LodgeReservation.Status.PENDING)
        validate_lodge_stay(
            lodge=attrs["lodge"],
            check_in=attrs["check_in"],
            check_out=attrs["check_out"],
            adults=attrs.get("adults", 1),
            children=attrs.get("children", 0),
            status=status_value,
        )
        return attrs


class StaffReservationUpdateSerializer(serializers.ModelSerializer):
    cancellation_reason = serializers.CharField(
        required=False, allow_blank=True, max_length=1000
    )
    lodge = serializers.PrimaryKeyRelatedField(
        queryset=Lodge.objects.all(), required=False
    )
    status = serializers.ChoiceField(
        choices=LodgeReservation.Status.choices, required=False
    )
    phone = serializers.RegexField(
        regex=r"^\+?[0-9][0-9\s().-]{5,38}[0-9]$", max_length=40, required=False
    )

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
            "cancellation_reason",
            "occasion",
            "source",
            "created_at",
            "updated_at",
        )
        read_only_fields = (
            "id",
            "reference",
            "lodge_name",
            "created_at",
            "updated_at",
        )

    lodge_name = serializers.CharField(source="lodge.name", read_only=True)

    def validate(self, attrs):
        reservation = self.instance
        if reservation is None:
            return attrs

        if (
            attrs.get("status", reservation.status)
            == LodgeReservation.Status.CANCELLED
            and reservation.status != LodgeReservation.Status.CANCELLED
            and not attrs.get(
                "cancellation_reason", reservation.cancellation_reason
            ).strip()
        ):
            raise serializers.ValidationError(
                {
                    "cancellation_reason": (
                        "A reason is required to cancel the reservation."
                    )
                }
            )

        lodge = attrs.get("lodge", reservation.lodge)
        check_in = attrs.get("check_in", reservation.check_in)
        check_out = attrs.get("check_out", reservation.check_out)
        adults = attrs.get("adults", reservation.adults)
        children = attrs.get("children", reservation.children)

        validate_lodge_stay(
            lodge=lodge,
            check_in=check_in,
            check_out=check_out,
            adults=adults,
            children=children,
            status=attrs.get("status", reservation.status),
            exclude_reservation_id=reservation.pk,
        )
        return attrs


PUSH_ENDPOINT_SUFFIXES = (
    "fcm.googleapis.com",
    "push.services.mozilla.com",
    "push.apple.com",
    "notify.windows.com",
)


class PushEndpointField(serializers.URLField):
    def __init__(self, **kwargs):
        kwargs.setdefault("max_length", 2048)
        super().__init__(**kwargs)

    def to_internal_value(self, data):
        endpoint = super().to_internal_value(data)
        parsed = urlsplit(endpoint)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme != "https" or not any(
            hostname == suffix or hostname.endswith(f".{suffix}")
            for suffix in PUSH_ENDPOINT_SUFFIXES
        ):
            raise serializers.ValidationError(
                "Use a secure browser push-service endpoint."
            )
        return endpoint


class StaffPushSubscriptionSerializer(serializers.Serializer):
    endpoint = PushEndpointField()
    keys = serializers.DictField(child=serializers.CharField(max_length=255))

    def validate_keys(self, value):
        if not value.get("p256dh") or not value.get("auth"):
            raise serializers.ValidationError(
                "Both p256dh and auth subscription keys are required."
            )
        return value


class StaffPushSubscriptionDeleteSerializer(serializers.Serializer):
    endpoint = PushEndpointField()
