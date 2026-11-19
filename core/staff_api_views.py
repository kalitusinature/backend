from django.contrib.auth import authenticate
from django.db import transaction
from django.db.models import Count, Q
from django.utils.dateparse import parse_date
from rest_framework import generics, status
from rest_framework.authentication import TokenAuthentication
from rest_framework.authtoken.models import Token
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny, IsAdminUser
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from .models import Category, Lodge, LodgeBlockout, LodgeReservation, Order, Product
from .reservation_notifications import (
    notify_new_reservation,
    notify_reservation_status,
)
from .reservation_rules import validate_lodge_stay
from .staff_api_serializers import (
    StaffCategorySerializer,
    StaffLoginSerializer,
    StaffLodgeBlockoutSerializer,
    StaffLodgeSerializer,
    StaffOrderSerializer,
    StaffOrderUpdateSerializer,
    StaffProductSerializer,
    StaffReservationCreateSerializer,
    StaffReservationSerializer,
    StaffReservationUpdateSerializer,
)


class StaffApiMixin:
    authentication_classes = (TokenAuthentication,)
    permission_classes = (IsAdminUser,)


class StaffReservationPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100


def validate_blockout_conflicts(lodge, start_date, end_date):
    if LodgeReservation.objects.filter(
        lodge=lodge,
        status__in=(
            LodgeReservation.Status.PENDING,
            LodgeReservation.Status.CONFIRMED,
        ),
        check_in__lt=end_date,
        check_out__gt=start_date,
    ).exists():
        raise ValidationError(
            {"start_date": "This period overlaps an active reservation. Resolve it first."}
        )


class StaffLogin(APIView):
    authentication_classes = ()
    permission_classes = (AllowAny,)
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = "staff_login"

    def post(self, request):
        serializer = StaffLoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = authenticate(
            request,
            username=serializer.validated_data["username"],
            password=serializer.validated_data["password"],
        )
        if user is None or not user.is_staff:
            return Response(
                {"detail": "Invalid staff credentials."},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        token, _ = Token.objects.get_or_create(user=user)
        return Response({"token": token.key, "username": user.get_username()})


class StaffLogout(StaffApiMixin, APIView):
    def post(self, request):
        request.auth.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class StaffDashboard(StaffApiMixin, APIView):
    def get(self, request):
        order_status_counts = {
            entry["status"]: entry["count"]
            for entry in Order.objects.values("status").annotate(count=Count("id"))
        }
        reservation_status_counts = {
            entry["status"]: entry["count"]
            for entry in LodgeReservation.objects.values("status").annotate(
                count=Count("id")
            )
        }
        return Response(
            {
                "orders_total": Order.objects.count(),
                "orders_by_status": order_status_counts,
                "new_orders": Order.objects.filter(status=Order.Status.NEW).count(),
                "reservations_total": sum(reservation_status_counts.values()),
                "reservations_by_status": reservation_status_counts,
                "pending_reservations": reservation_status_counts.get(
                    LodgeReservation.Status.PENDING, 0
                ),
                "products_total": Product.objects.count(),
                "categories_total": Category.objects.count(),
                "lodges_total": Lodge.objects.count(),
                "recent_orders": StaffOrderSerializer(
                    Order.objects.prefetch_related("items")[:5], many=True
                ).data,
                "recent_reservations": StaffReservationSerializer(
                    LodgeReservation.objects.select_related("lodge")[:5], many=True
                ).data,
            }
        )


class StaffOrderList(StaffApiMixin, generics.ListAPIView):
    serializer_class = StaffOrderSerializer

    def get_queryset(self):
        queryset = Order.objects.prefetch_related("items")
        status_filter = self.request.query_params.get("status")
        if status_filter and status_filter in Order.Status.values:
            queryset = queryset.filter(status=status_filter)
        query = self.request.query_params.get("q", "").strip()
        if query:
            queryset = queryset.filter(number__icontains=query) | queryset.filter(
                guest_name__icontains=query
            )
        return queryset


class StaffOrderUpdate(StaffApiMixin, generics.UpdateAPIView):
    queryset = Order.objects.select_for_update()
    serializer_class = StaffOrderUpdateSerializer
    http_method_names = ("patch", "options", "head")

    @transaction.atomic
    def update(self, request, *args, **kwargs):
        return super().update(request, *args, **kwargs)


class StaffProductListCreate(StaffApiMixin, generics.ListCreateAPIView):
    queryset = Product.objects.select_related("category")
    serializer_class = StaffProductSerializer


class StaffProductDetail(StaffApiMixin, generics.RetrieveUpdateAPIView):
    queryset = Product.objects.select_related("category")
    serializer_class = StaffProductSerializer
    http_method_names = ("get", "patch", "put", "options", "head")


class StaffCategoryListCreate(StaffApiMixin, generics.ListCreateAPIView):
    queryset = Category.objects.all()
    serializer_class = StaffCategorySerializer


class StaffCategoryDetail(StaffApiMixin, generics.RetrieveUpdateAPIView):
    queryset = Category.objects.all()
    serializer_class = StaffCategorySerializer
    http_method_names = ("get", "patch", "put", "options", "head")


class StaffLodgeListCreate(StaffApiMixin, generics.ListCreateAPIView):
    queryset = Lodge.objects.all()
    serializer_class = StaffLodgeSerializer


class StaffLodgeDetail(StaffApiMixin, generics.RetrieveUpdateAPIView):
    queryset = Lodge.objects.all()
    serializer_class = StaffLodgeSerializer
    http_method_names = ("get", "patch", "put", "options", "head")


class StaffReservationList(StaffApiMixin, generics.ListCreateAPIView):
    pagination_class = StaffReservationPagination

    def get_serializer_class(self):
        if self.request.method == "POST":
            return StaffReservationCreateSerializer
        return StaffReservationSerializer

    def get_queryset(self):
        queryset = LodgeReservation.objects.select_related("lodge")
        status_filter = self.request.query_params.get("status")
        if status_filter and status_filter != "all":
            if status_filter not in LodgeReservation.Status.values:
                raise ValidationError({"status": "Unknown reservation status."})
            queryset = queryset.filter(status=status_filter)
        source_filter = self.request.query_params.get("source")
        if source_filter:
            if source_filter not in LodgeReservation.Source.values:
                raise ValidationError({"source": "Unknown reservation source."})
            queryset = queryset.filter(source=source_filter)
        lodge_filter = self.request.query_params.get("lodge")
        if lodge_filter:
            try:
                lodge_id = int(lodge_filter)
            except ValueError as error:
                raise ValidationError({"lodge": "Provide a valid lodge ID."}) from error
            queryset = queryset.filter(lodge_id=lodge_id)
        query = self.request.query_params.get("q", "").strip()
        if query:
            queryset = queryset.filter(
                Q(guest_name__icontains=query)
                | Q(phone__icontains=query)
                | Q(reference__icontains=query)
            )
        start_value = self.request.query_params.get("start_date")
        end_value = self.request.query_params.get("end_date")
        start = parse_date(start_value) if start_value else None
        end = parse_date(end_value) if end_value else None
        if start_value and not start:
            raise ValidationError({"start_date": "Use YYYY-MM-DD format."})
        if end_value and not end:
            raise ValidationError({"end_date": "Use YYYY-MM-DD format."})
        if start and end and end < start:
            raise ValidationError({"end_date": "End date must not be before start date."})
        if start:
            queryset = queryset.filter(check_out__gt=start)
        if end:
            queryset = queryset.filter(check_in__lte=end)
        ordering = self.request.query_params.get("ordering", "-created_at")
        allowed_ordering = {
            "check_in",
            "-check_in",
            "created_at",
            "-created_at",
        }
        if ordering not in allowed_ordering:
            raise ValidationError(
                {"ordering": "Use check_in, -check_in, created_at, or -created_at."}
            )
        queryset = queryset.order_by(ordering, "id")
        return queryset

    @transaction.atomic
    def perform_create(self, serializer):
        data = serializer.validated_data
        lodge = Lodge.objects.select_for_update().get(pk=data["lodge"].pk)
        status_value = data.get("status", LodgeReservation.Status.PENDING)
        validate_lodge_stay(
            lodge=lodge,
            check_in=data["check_in"],
            check_out=data["check_out"],
            adults=data.get("adults", 1),
            children=data.get("children", 0),
            status=status_value,
        )
        reservation = serializer.save()
        transaction.on_commit(lambda: notify_new_reservation(reservation))


class StaffReservationCalendar(StaffApiMixin, APIView):
    def get(self, request):
        start_value = request.query_params.get("start")
        end_value = request.query_params.get("end")
        start = parse_date(start_value) if start_value else None
        end = parse_date(end_value) if end_value else None
        if not start or not end or end <= start:
            raise ValidationError(
                {"dates": "Provide a valid start and exclusive end date."}
            )
        if (end - start).days > 93:
            raise ValidationError({"end": "Calendar range cannot exceed 93 days."})
        reservations = LodgeReservation.objects.filter(
            check_in__lt=end,
            check_out__gt=start,
        ).select_related("lodge").order_by("check_in", "created_at")
        return Response(StaffReservationSerializer(reservations, many=True).data)


class StaffLodgeBlockoutListCreate(StaffApiMixin, generics.ListCreateAPIView):
    queryset = LodgeBlockout.objects.select_related("lodge")
    serializer_class = StaffLodgeBlockoutSerializer

    def get_queryset(self):
        queryset = super().get_queryset()
        lodge_id = self.request.query_params.get("lodge")
        if lodge_id:
            try:
                lodge_id = int(lodge_id)
            except ValueError as error:
                raise ValidationError({"lodge": "Provide a valid lodge ID."}) from error
            queryset = queryset.filter(lodge_id=lodge_id)
        return queryset

    @transaction.atomic
    def perform_create(self, serializer):
        data = serializer.validated_data
        lodge = Lodge.objects.select_for_update().get(pk=data["lodge"].pk)
        validate_blockout_conflicts(lodge, data["start_date"], data["end_date"])
        serializer.save(lodge=lodge)


class StaffLodgeBlockoutDetail(StaffApiMixin, generics.RetrieveUpdateDestroyAPIView):
    queryset = LodgeBlockout.objects.select_related("lodge")
    serializer_class = StaffLodgeBlockoutSerializer
    http_method_names = ("get", "patch", "put", "delete", "options", "head")

    @transaction.atomic
    def perform_update(self, serializer):
        blockout = LodgeBlockout.objects.select_for_update().get(pk=self.get_object().pk)
        lodge = serializer.validated_data.get("lodge", blockout.lodge)
        lodge = Lodge.objects.select_for_update().get(pk=lodge.pk)
        start_date = serializer.validated_data.get("start_date", blockout.start_date)
        end_date = serializer.validated_data.get("end_date", blockout.end_date)
        validate_blockout_conflicts(lodge, start_date, end_date)
        serializer.instance = blockout
        serializer.save(lodge=lodge)


class StaffReservationUpdate(StaffApiMixin, generics.RetrieveUpdateDestroyAPIView):
    queryset = LodgeReservation.objects.select_related("lodge")
    serializer_class = StaffReservationUpdateSerializer
    http_method_names = ("get", "patch", "put", "delete", "options", "head")

    @transaction.atomic
    def update(self, request, *args, **kwargs):
        return super().update(request, *args, **kwargs)

    @transaction.atomic
    def perform_update(self, serializer):
        reservation = LodgeReservation.objects.select_for_update().get(
            pk=self.get_object().pk
        )
        lodge = serializer.validated_data.get("lodge", reservation.lodge)
        lodge = Lodge.objects.select_for_update().get(pk=lodge.pk)
        status_value = serializer.validated_data.get("status", reservation.status)
        check_in = serializer.validated_data.get("check_in", reservation.check_in)
        check_out = serializer.validated_data.get("check_out", reservation.check_out)
        adults = serializer.validated_data.get("adults", reservation.adults)
        children = serializer.validated_data.get("children", reservation.children)
        validate_lodge_stay(
            lodge=lodge,
            check_in=check_in,
            check_out=check_out,
            adults=adults,
            children=children,
            status=status_value,
            exclude_reservation_id=reservation.pk,
        )
        previous_status = reservation.status
        serializer.instance = reservation
        updated = serializer.save()
        if updated.status != previous_status:
            transaction.on_commit(lambda: notify_reservation_status(updated))

    def perform_destroy(self, instance):
        if instance.status in (
            LodgeReservation.Status.PENDING,
            LodgeReservation.Status.CONFIRMED,
        ):
            raise ValidationError(
                {"status": "Decline or cancel an active reservation before deleting it."}
            )
        instance.delete()
