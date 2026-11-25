from django.db import IntegrityError, transaction
from django.db.models import Avg, Count
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle, ScopedRateThrottle
from rest_framework.views import APIView

from .models import CustomerReview, Lodge, Product
from .review_serializers import (
    CustomerReviewSerializer,
    ReviewAccessSubmitSerializer,
)
from .review_tokens import resolve_review_link


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

class ProductReviewListCreate(PublicReviewListCreate):
    def get(self, request, slug):
        product = get_object_or_404(Product, slug=slug)
        return Response(self.serialize_reviews(product.reviews.all()))


class LodgeReviewListCreate(PublicReviewListCreate):
    def get(self, request, slug):
        lodge = get_object_or_404(Lodge, slug=slug)
        return Response(self.serialize_reviews(lodge.reviews.all()))


class ReviewAccessView(PublicReviewListCreate):
    def get(self, request):
        context = resolve_review_link(request.query_params.get("token", ""))
        if context is None:
            raise NotFound("This review link is invalid or no longer eligible.")
        target = context["target"]
        return Response(
            {
                "kind": context["kind"],
                "name": target.name,
                "slug": target.slug,
                "can_review": context["review"] is None,
                **self.serialize_reviews(target.reviews.all()),
            }
        )

    def post(self, request):
        serializer = ReviewAccessSubmitSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        token = serializer.validated_data["token"]
        with transaction.atomic():
            context = resolve_review_link(token, for_update=True)
            if context is None:
                raise NotFound("This review link is invalid or no longer eligible.")
            if context["review"] is not None:
                raise ValidationError(
                    {"detail": "This completed visit was already reviewed."}
                )

            review_fields = {
                "reviewer_name": (
                    context["order"].guest_name
                    if context["kind"] == "product"
                    else context["reservation"].guest_name
                ),
                "rating": serializer.validated_data["rating"],
                "comment": serializer.validated_data["comment"],
            }
            if context["kind"] == "product":
                review_fields.update(
                    product=context["target"],
                    order=context["order"],
                )
            else:
                review_fields.update(
                    lodge=context["target"],
                    reservation=context["reservation"],
                )

            review_lookup = {
                key: review_fields[key]
                for key in ("product", "order", "lodge", "reservation")
                if key in review_fields
            }
            try:
                with transaction.atomic():
                    review = CustomerReview.objects.create(**review_fields)
            except IntegrityError:
                if not CustomerReview.objects.filter(**review_lookup).exists():
                    raise
                raise ValidationError(
                    {"detail": "This completed visit was already reviewed."}
                )
        return Response(
            CustomerReviewSerializer(review).data,
            status=status.HTTP_201_CREATED,
        )
