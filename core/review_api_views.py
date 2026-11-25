from django.db import IntegrityError, transaction
from django.db.models import Avg, Count
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle, ScopedRateThrottle
from rest_framework.views import APIView

from .models import Lodge, Product
from .review_serializers import (
    CustomerReviewSerializer,
    LodgeReviewCreateSerializer,
    ProductReviewCreateSerializer,
)


class PublicReviewListCreate(APIView):
    authentication_classes = ()
    permission_classes = (AllowAny,)
    throttle_scope = "review_create"

    def get_throttles(self):
        if self.request.method == "POST":
            return [AnonRateThrottle(), ScopedRateThrottle()]
        return []

    @staticmethod
    def serialize_reviews(reviews):
        summary = reviews.aggregate(
            average_rating=Avg("rating"),
            review_count=Count("id"),
        )
        return {
            "average_rating": summary["average_rating"],
            "review_count": summary["review_count"],
            "reviews": CustomerReviewSerializer(reviews[:100], many=True).data,
        }

    @staticmethod
    def create_review(serializer, duplicate_message):
        serializer.is_valid(raise_exception=True)
        try:
            with transaction.atomic():
                review = serializer.save()
        except IntegrityError:
            duplicate_fields = (
                "product",
                "order",
                "lodge",
                "reservation",
            )
            duplicate_lookup = {
                field: serializer.validated_data[field]
                for field in duplicate_fields
                if field in serializer.validated_data
            }
            if not CustomerReview.objects.filter(**duplicate_lookup).exists():
                raise
            raise ValidationError({"detail": duplicate_message})
        return Response(
            CustomerReviewSerializer(review).data,
            status=status.HTTP_201_CREATED,
        )


class ProductReviewListCreate(PublicReviewListCreate):
    def get(self, request, slug):
        product = get_object_or_404(Product, slug=slug)
        return Response(self.serialize_reviews(product.reviews.all()))

    def post(self, request, slug):
        product = get_object_or_404(Product, slug=slug)
        serializer = ProductReviewCreateSerializer(
            data=request.data,
            context={"product": product},
        )
        return self.create_review(
            serializer,
            "This order has already reviewed this item.",
        )


class LodgeReviewListCreate(PublicReviewListCreate):
    def get(self, request, slug):
        lodge = get_object_or_404(Lodge, slug=slug)
        return Response(self.serialize_reviews(lodge.reviews.all()))

    def post(self, request, slug):
        lodge = get_object_or_404(Lodge, slug=slug)
        serializer = LodgeReviewCreateSerializer(
            data=request.data,
            context={"lodge": lodge},
        )
        return self.create_review(
            serializer,
            "This stay has already reviewed this cottage.",
        )
