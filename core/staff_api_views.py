from django.contrib.auth import authenticate
from django.db import transaction
from django.db.models import Count
from rest_framework import generics, status
from rest_framework.authentication import TokenAuthentication
from rest_framework.authtoken.models import Token
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny, IsAdminUser
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from .models import Category, Lodge, LodgeReservation, Order, Product
from .staff_api_serializers import (
    StaffCategorySerializer,
    StaffLoginSerializer,
    StaffLodgeSerializer,
    StaffOrderSerializer,
    StaffOrderUpdateSerializer,
    StaffProductSerializer,
    StaffReservationSerializer,
    StaffReservationStatusSerializer,
)


class StaffApiMixin:
    authentication_classes = (TokenAuthentication,)
    permission_classes = (IsAdminUser,)


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
        return Response(
            {
                "orders_total": Order.objects.count(),
                "orders_by_status": order_status_counts,
                "new_orders": Order.objects.filter(status=Order.Status.NEW).count(),
                "pending_reservations": LodgeReservation.objects.filter(
                    status=LodgeReservation.Status.PENDING
                ).count(),
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
    queryset = Order.objects.all()
    serializer_class = StaffOrderUpdateSerializer
    http_method_names = ("patch", "options", "head")


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


class StaffReservationList(StaffApiMixin, generics.ListAPIView):
    serializer_class = StaffReservationSerializer

    def get_queryset(self):
        queryset = LodgeReservation.objects.select_related("lodge")
        status_filter = self.request.query_params.get("status")
        if status_filter and status_filter in LodgeReservation.Status.values:
            queryset = queryset.filter(status=status_filter)
        return queryset


class StaffReservationUpdate(StaffApiMixin, generics.UpdateAPIView):
    queryset = LodgeReservation.objects.select_related("lodge")
    serializer_class = StaffReservationStatusSerializer
    http_method_names = ("patch", "options", "head")

    @transaction.atomic
    def perform_update(self, serializer):
        reservation = LodgeReservation.objects.select_for_update().get(
            pk=self.get_object().pk
        )
        status_value = serializer.validated_data["status"]
        if status_value == LodgeReservation.Status.CONFIRMED:
            Lodge.objects.select_for_update().get(pk=reservation.lodge_id)
            conflict = LodgeReservation.objects.filter(
                lodge_id=reservation.lodge_id,
                status=LodgeReservation.Status.CONFIRMED,
                check_in__lt=reservation.check_out,
                check_out__gt=reservation.check_in,
            ).exclude(pk=reservation.pk).exists()
            if conflict:
                raise ValidationError(
                    {"status": "Cannot confirm: this lodge has an overlapping reservation."}
                )
        reservation.status = status_value
        reservation.save(update_fields=["status"])
        serializer.instance = reservation
