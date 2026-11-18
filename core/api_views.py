from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils.dateparse import parse_date
from rest_framework import generics, status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from .models import (
    Category,
    Lodge,
    LodgeReservation,
    Order,
    OrderItem,
    Product,
)
from .serializers import (
    CategorySerializer,
    LodgeSerializer,
    OrderCreateSerializer,
    OrderReceiptSerializer,
    OrderTrackingRequestSerializer,
    OrderTrackingSerializer,
    ProductSerializer,
    ReservationCreateSerializer,
    ReservationReceiptSerializer,
)


class CategoryList(generics.ListAPIView):
    serializer_class = CategorySerializer
    queryset = Category.objects.filter(is_active=True)


class ProductList(generics.ListAPIView):
    serializer_class = ProductSerializer

    def get_queryset(self):
        queryset = Product.objects.filter(
            is_available=True, category__is_active=True
        ).select_related("category")
        category = self.request.query_params.get("category")
        if category:
            queryset = queryset.filter(category__slug=category)
        featured = self.request.query_params.get("featured")
        if featured == "true":
            queryset = queryset.filter(is_featured=True)
        return queryset


class LodgeList(generics.ListAPIView):
    serializer_class = LodgeSerializer

    def get_queryset(self):
        queryset = Lodge.objects.filter(is_active=True)
        check_in_value = self.request.query_params.get("check_in")
        check_out_value = self.request.query_params.get("check_out")
        if bool(check_in_value) != bool(check_out_value):
            raise ValidationError(
                {"dates": "Provide both check_in and check_out in YYYY-MM-DD format."}
            )
        if check_in_value and check_out_value:
            check_in = parse_date(check_in_value)
            check_out = parse_date(check_out_value)
            if not check_in or not check_out or check_out <= check_in:
                raise ValidationError(
                    {"dates": "Provide valid dates and ensure check_out is after check_in."}
                )
            booked_ids = LodgeReservation.objects.filter(
                status=LodgeReservation.Status.CONFIRMED,
                check_in__lt=check_out,
                check_out__gt=check_in,
            ).values_list("lodge_id", flat=True)
            queryset = queryset.exclude(id__in=booked_ids)
        return queryset


class OrderCreate(APIView):
    throttle_scope = "order_create"

    @transaction.atomic
    def post(self, request):
        serializer = OrderCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        items = data.pop("items")
        order = Order.objects.create(**data)
        total = 0
        for line in items:
            product = line["product"]
            quantity = line["quantity"]
            line_total = product.price_ugx * quantity
            OrderItem.objects.create(
                order=order,
                product=product,
                product_name=product.name,
                unit_price_ugx=product.price_ugx,
                quantity=quantity,
                line_total_ugx=line_total,
            )
            total += line_total
        order.total_ugx = total
        order.save(update_fields=["total_ugx", "updated_at"])
        return Response(
            OrderReceiptSerializer(order).data, status=status.HTTP_201_CREATED
        )


class OrderTrack(APIView):
    authentication_classes = ()
    permission_classes = (AllowAny,)
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = "order_track"

    def post(self, request):
        serializer = OrderTrackingRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        order = get_object_or_404(
            Order.objects.prefetch_related("items"),
            number__iexact=serializer.validated_data["number"].strip(),
            phone=serializer.validated_data["phone"],
        )
        return Response(OrderTrackingSerializer(order).data)


class ReservationCreate(APIView):
    throttle_scope = "reservation_create"

    @transaction.atomic
    def post(self, request):
        serializer = ReservationCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        reservation = serializer.save()
        return Response(
            ReservationReceiptSerializer(reservation).data,
            status=status.HTTP_201_CREATED,
        )
