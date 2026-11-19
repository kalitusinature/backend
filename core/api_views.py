from calendar import monthrange
from datetime import date, datetime, timedelta

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils.dateparse import parse_date
from django.utils import timezone
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
from .reservation_rules import validate_lodge_stay
from .reservation_notifications import notify_new_reservation
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

ORDER_TRACKING_RETENTION = timedelta(days=2)


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
            guests_value = self.request.query_params.get("guests", "1")
            try:
                guests = int(guests_value)
            except ValueError as error:
                raise ValidationError({"guests": "Provide a valid guest count."}) from error
            if guests < 1 or guests > 50:
                raise ValidationError({"guests": "Guest count must be between 1 and 50."})

            available_ids = []
            for lodge in queryset:
                try:
                    validate_lodge_stay(
                        lodge=lodge,
                        check_in=check_in,
                        check_out=check_out,
                        adults=guests,
                        children=0,
                        status=LodgeReservation.Status.PENDING,
                    )
                except ValidationError:
                    continue
                available_ids.append(lodge.pk)
            queryset = queryset.filter(pk__in=available_ids)
        return queryset


class LodgeAvailability(APIView):
    authentication_classes = ()
    permission_classes = (AllowAny,)

    def get(self, request, slug):
        lodge = get_object_or_404(Lodge, slug=slug, is_active=True)
        month_value = request.query_params.get(
            "month", timezone.localdate().strftime("%Y-%m")
        )
        try:
            month_start = datetime.strptime(month_value, "%Y-%m").date()
        except ValueError as error:
            raise ValidationError({"month": "Use YYYY-MM format."}) from error
        if month_start.strftime("%Y-%m") != month_value:
            raise ValidationError({"month": "Use YYYY-MM format."})

        try:
            nights = int(request.query_params.get("nights", lodge.min_nights))
        except ValueError as error:
            raise ValidationError({"nights": "Provide a valid number of nights."}) from error
        if nights < lodge.min_nights or nights > lodge.max_nights:
            raise ValidationError(
                {
                    "nights": (
                        f"Choose between {lodge.min_nights} and "
                        f"{lodge.max_nights} nights."
                    )
                }
            )

        try:
            guests = int(request.query_params.get("guests", "1"))
        except ValueError as error:
            raise ValidationError({"guests": "Provide a valid guest count."}) from error
        if guests < 1 or guests > lodge.capacity:
            raise ValidationError(
                {"guests": f"Guest count must be between 1 and {lodge.capacity}."}
            )

        days_in_month = monthrange(month_start.year, month_start.month)[1]
        search_end = date(
            month_start.year, month_start.month, days_in_month
        ) + timedelta(days=lodge.max_nights)
        reservation_periods = list(
            LodgeReservation.objects.filter(
                lodge=lodge,
                status__in=(
                    LodgeReservation.Status.PENDING,
                    LodgeReservation.Status.CONFIRMED,
                ),
                check_in__lt=search_end,
                check_out__gt=month_start,
            ).values_list("pk", "check_in", "check_out")
        )
        blockout_periods = list(
            lodge.blockouts.filter(
                start_date__lt=search_end,
                end_date__gt=month_start,
            ).values_list("start_date", "end_date")
        )

        available_dates = []
        unavailable_dates = []
        for day in range(days_in_month):
            check_in = month_start + timedelta(days=day)
            try:
                validate_lodge_stay(
                    lodge=lodge,
                    check_in=check_in,
                    check_out=check_in + timedelta(days=nights),
                    adults=guests,
                    children=0,
                    status=LodgeReservation.Status.PENDING,
                    reservation_periods=reservation_periods,
                    blockout_periods=blockout_periods,
                )
            except ValidationError:
                unavailable_dates.append(check_in.isoformat())
            else:
                available_dates.append(check_in.isoformat())

        return Response(
            {
                "lodge": lodge.slug,
                "month": month_value,
                "nights": nights,
                "min_nights": lodge.min_nights,
                "max_nights": lodge.max_nights,
                "capacity": lodge.capacity,
                "available_dates": available_dates,
                "unavailable_dates": unavailable_dates,
            }
        )


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
            email__iexact=serializer.validated_data["email"],
        )
        if order.created_at <= timezone.now() - ORDER_TRACKING_RETENTION:
            return Response(
                {"detail": "Online order tracking is available for two days after placement."},
                status=status.HTTP_410_GONE,
            )
        return Response(OrderTrackingSerializer(order).data)


class ReservationCreate(APIView):
    throttle_scope = "reservation_create"

    @transaction.atomic
    def post(self, request):
        serializer = ReservationCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        lodge = Lodge.objects.select_for_update().get(
            pk=serializer.validated_data["lodge"].pk
        )
        validate_lodge_stay(
            lodge=lodge,
            check_in=serializer.validated_data["check_in"],
            check_out=serializer.validated_data["check_out"],
            adults=serializer.validated_data["adults"],
            children=serializer.validated_data["children"],
            status=LodgeReservation.Status.PENDING,
        )
        reservation = serializer.save()
        transaction.on_commit(lambda: notify_new_reservation(reservation))
        return Response(
            ReservationReceiptSerializer(reservation).data,
            status=status.HTTP_201_CREATED,
        )
