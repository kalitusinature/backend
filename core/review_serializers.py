from rest_framework import serializers

from .models import (
    CustomerReview,
    LodgeReservation,
    Order,
)


class CustomerReviewSerializer(serializers.ModelSerializer):
    class Meta:
        model = CustomerReview
        fields = ("id", "reviewer_name", "rating", "comment", "created_at")
        read_only_fields = fields


class ProductReviewCreateSerializer(serializers.Serializer):
    order_number = serializers.CharField(max_length=20, trim_whitespace=True)
    email = serializers.EmailField(max_length=254)
    rating = serializers.IntegerField(min_value=1, max_value=5)
    comment = serializers.CharField(max_length=2000, trim_whitespace=True)

    def validate(self, attrs):
        product = self.context["product"]
        order = (
            Order.objects.filter(
                number__iexact=attrs["order_number"].strip(),
                email__iexact=attrs["email"].strip(),
                status=Order.Status.COMPLETED,
                items__product=product,
            )
            .distinct()
            .first()
        )
        if order is None:
            raise serializers.ValidationError(
                {
                    "order_number": (
                        "A matching completed order containing this item is required."
                    )
                }
            )
        if CustomerReview.objects.filter(order=order, product=product).exists():
            raise serializers.ValidationError(
                {"order_number": "This order has already reviewed this item."}
            )
        attrs.pop("order_number")
        attrs.pop("email")
        attrs.update(
            product=product,
            order=order,
            reviewer_name=order.guest_name,
        )
        return attrs

    def create(self, validated_data):
        return CustomerReview.objects.create(**validated_data)


class LodgeReviewCreateSerializer(serializers.Serializer):
    reservation_reference = serializers.CharField(max_length=20, trim_whitespace=True)
    email = serializers.EmailField(max_length=254)
    rating = serializers.IntegerField(min_value=1, max_value=5)
    comment = serializers.CharField(max_length=2000, trim_whitespace=True)

    def validate(self, attrs):
        lodge = self.context["lodge"]
        reservation = LodgeReservation.objects.filter(
            reference__iexact=attrs["reservation_reference"].strip(),
            email__iexact=attrs["email"].strip(),
            lodge=lodge,
            status=LodgeReservation.Status.COMPLETED,
        ).first()
        if reservation is None:
            raise serializers.ValidationError(
                {
                    "reservation_reference": (
                        "A matching checked-out stay at this cottage is required."
                    )
                }
            )
        if CustomerReview.objects.filter(
            reservation=reservation,
            lodge=lodge,
        ).exists():
            raise serializers.ValidationError(
                {"reservation_reference": "This stay has already reviewed this cottage."}
            )
        attrs.pop("reservation_reference")
        attrs.pop("email")
        attrs.update(
            lodge=lodge,
            reservation=reservation,
            reviewer_name=reservation.guest_name,
        )
        return attrs

    def create(self, validated_data):
        return CustomerReview.objects.create(**validated_data)
