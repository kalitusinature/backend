import hashlib
import secrets
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
    GuestCart,
    GuestCartItem,
    Lodge,
    LodgeReservation,
    Order,
    OrderItem,
    Product,
)
from .reservation_rules import (
    BOOKED_RESERVATION_STATUSES,
    validate_lodge_stay,
)
from .reservation_notifications import notify_new_reservation
from .email_notifications import notify_order_created
from .push_notifications import (
    notify_new_order,
    notify_new_reservation as notify_staff_new_reservation,
)
from .serializers import (
    CategorySerializer,
    GuestCartItemAddSerializer,
    GuestCartItemQuantitySerializer,
    GuestCartSerializer,
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


class GuestCartAccessMixin:
    authentication_classes = ()
    permission_classes = (AllowAny,)

    def get_cart(self, request):
        authorization = request.headers.get("Authorization", "")
        scheme, separator, token = authorization.partition(" ")
        if not separator or scheme.lower() != "bearer" or not token.strip():
            return None
        digest = hashlib.sha256(token.strip().encode("utf-8")).hexdigest()
        return (
            GuestCart.objects.filter(token_digest=digest)
            .prefetch_related("items__product")
            .first()
        )

    def unauthorized(self):
        return Response(
            {"detail": "A valid cart token is required."},
            status=status.HTTP_401_UNAUTHORIZED,
        )


class GuestCartCreate(APIView):
    authentication_classes = ()
    permission_classes = (AllowAny,)

    @transaction.atomic
    def post(self, request):
        if not isinstance(request.data, dict):
            raise ValidationError({"detail": "Expected a JSON object."})
        serializer = GuestCartItemAddSerializer(
            data=request.data.get("items", []), many=True
        )
        serializer.is_valid(raise_exception=True)
        quantities = {}
        products = {}
        for item in serializer.validated_data:
            product = item["product"]
            products[product.pk] = product
            quantities[product.pk] = quantities.get(product.pk, 0) + item["quantity"]
            if quantities[product.pk] > 99:
                raise ValidationError(
                    {"items": "The maximum quantity for an item is 99."}
                )
        token = secrets.token_urlsafe(32)
        cart = GuestCart.objects.create(
            token_digest=hashlib.sha256(token.encode("utf-8")).hexdigest()
        )
        GuestCartItem.objects.bulk_create(
            [
                GuestCartItem(cart=cart, product=products[product_id], quantity=quantity)
                for product_id, quantity in quantities.items()
            ]
        )
        data = GuestCartSerializer(cart).data
        data["token"] = token
        return Response(data, status=status.HTTP_201_CREATED)


class GuestCartView(GuestCartAccessMixin, APIView):
    def get(self, request):
        cart = self.get_cart(request)
        if cart is None:
            return self.unauthorized()
        return Response(GuestCartSerializer(cart).data)

    def delete(self, request):
        cart = self.get_cart(request)
        if cart is None:
            return self.unauthorized()
        cart.items.all().delete()
        cart = GuestCart.objects.prefetch_related("items__product").get(pk=cart.pk)
        return Response(GuestCartSerializer(cart).data)


class GuestCartItemCreate(GuestCartAccessMixin, APIView):
    @transaction.atomic
    def post(self, request):
        cart = self.get_cart(request)
        if cart is None:
            return self.unauthorized()
        serializer = GuestCartItemAddSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        product = serializer.validated_data["product"]
        quantity = serializer.validated_data["quantity"]
        cart = GuestCart.objects.select_for_update().get(pk=cart.pk)
        item = GuestCartItem.objects.filter(cart=cart, product=product).first()
        current_quantity = item.quantity if item else 0
        if current_quantity + quantity > 99:
            return Response(
                {"quantity": "The maximum quantity for an item is 99."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if item:
            item.quantity += quantity
            item.save(update_fields=["quantity"])
        else:
            GuestCartItem.objects.create(
                cart=cart, product=product, quantity=quantity
            )
        cart.save(update_fields=["updated_at"])
        return Response(
            GuestCartSerializer(cart).data, status=status.HTTP_200_OK
        )


class GuestCartItemDetail(GuestCartAccessMixin, APIView):
    def patch(self, request, product_slug):
        cart = self.get_cart(request)
        if cart is None:
            return self.unauthorized()
        serializer = GuestCartItemQuantitySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        item = cart.items.filter(product__slug=product_slug).first()
        if item is None:
            return Response(
                {"detail": "This product is not in your cart."},
                status=status.HTTP_404_NOT_FOUND,
            )
        quantity = serializer.validated_data["quantity"]
        if quantity == 0:
            item.delete()
        else:
            item.quantity = quantity
            item.save(update_fields=["quantity"])
        cart.save(update_fields=["updated_at"])
        cart = GuestCart.objects.prefetch_related("items__product").get(pk=cart.pk)
        return Response(GuestCartSerializer(cart).data)

    def delete(self, request, product_slug):
        cart = self.get_cart(request)
        if cart is None:
            return self.unauthorized()
        cart.items.filter(product__slug=product_slug).delete()
        cart.save(update_fields=["updated_at"])
        cart = GuestCart.objects.prefetch_related("items__product").get(pk=cart.pk)
        return Response(GuestCartSerializer(cart).data)


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
        queryset = Lodge.objects.filter(is_active=True).prefetch_related("images")
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
                        status=LodgeReservation.Status.CONFIRMED,
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
        lodge = get_object_or_404(
            Lodge.objects.prefetch_related("images"), slug=slug, is_active=True
        )
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
        check_in_value = request.query_params.get("check_in")
        selected_check_in = None
        if check_in_value:
            selected_check_in = parse_date(check_in_value)
            if not selected_check_in:
                raise ValidationError(
                    {"check_in": "Use YYYY-MM-DD format for check-in."}
                )

        month_end = date(
            month_start.year, month_start.month, days_in_month
        ) + timedelta(days=1)
        search_start = selected_check_in or month_start
        search_end = (
            month_end
            if selected_check_in
            else month_end + timedelta(days=lodge.max_nights)
        )
        reservation_periods = list(
            LodgeReservation.objects.filter(
                lodge=lodge,
                status__in=BOOKED_RESERVATION_STATUSES,
                check_in__lt=search_end,
                check_out__gt=search_start,
            ).values_list("pk", "check_in", "check_out")
        )
        blockout_periods = list(
            lodge.blockouts.filter(
                start_date__lt=search_end,
                end_date__gt=search_start,
            ).values_list("start_date", "end_date")
        )

        available_dates = []
        unavailable_dates = []
        booked_dates = []
        for day in range(days_in_month):
            candidate_date = month_start + timedelta(days=day)
            check_in = selected_check_in or candidate_date
            check_out = (
                candidate_date
                if selected_check_in
                else candidate_date + timedelta(days=nights)
            )
            is_booked = any(
                reservation_start < check_out and reservation_end > check_in
                for _, reservation_start, reservation_end in reservation_periods
            )
            try:
                validate_lodge_stay(
                    lodge=lodge,
                    check_in=check_in,
                    check_out=check_out,
                    adults=guests,
                    children=0,
                    status=LodgeReservation.Status.CONFIRMED,
                    reservation_periods=reservation_periods,
                    blockout_periods=blockout_periods,
                )
            except ValidationError:
                unavailable_dates.append(candidate_date.isoformat())
                if is_booked:
                    booked_dates.append(candidate_date.isoformat())
            else:
                available_dates.append(candidate_date.isoformat())

        response_data = {
            "lodge": lodge.slug,
            "month": month_value,
            "nights": nights,
            "min_nights": lodge.min_nights,
            "max_nights": lodge.max_nights,
            "capacity": lodge.capacity,
            "available_dates": available_dates,
            "unavailable_dates": unavailable_dates,
            "booked_dates": booked_dates,
        }
        if selected_check_in:
            response_data["check_in"] = selected_check_in.isoformat()
            response_data["available_check_out_dates"] = available_dates
            response_data["unavailable_check_out_dates"] = unavailable_dates
            response_data["booked_check_out_dates"] = booked_dates
        return Response(response_data)


class OrderCreate(APIView):
    throttle_scope = "order_create"

    @transaction.atomic
    def post(self, request):
        serializer = OrderCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        guest_cart = data.pop("_guest_cart", None)
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
        if guest_cart:
            guest_cart.items.all().delete()
        transaction.on_commit(lambda: notify_new_order(order), robust=True)
        transaction.on_commit(lambda: notify_order_created(order), robust=True)
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
            Order.objects.prefetch_related("items", "payments__receipt"),
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
        transaction.on_commit(
            lambda: notify_new_reservation(reservation),
            robust=True,
        )
        transaction.on_commit(
            lambda: notify_staff_new_reservation(reservation),
            robust=True,
        )
        return Response(
            ReservationReceiptSerializer(reservation).data,
            status=status.HTTP_201_CREATED,
        )
