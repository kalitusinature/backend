from django.conf import settings
from django.contrib.auth import authenticate, get_user_model, password_validation
from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from django.utils.dateparse import parse_date
from django.utils.crypto import get_random_string
from django.utils.encoding import force_str
from django.utils.http import urlsafe_base64_decode
from rest_framework import generics, status
from rest_framework.authentication import TokenAuthentication
from rest_framework.authtoken.models import Token
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny, IsAdminUser
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from .models import (
    Category,
    Lodge,
    LodgeBlockout,
    LodgeReservation,
    Order,
    Product,
    StaffProfile,
    StaffPushSubscription,
)
from .reservation_notifications import (
    notify_new_reservation,
    notify_reservation_status,
)
from .email_notifications import (
    notify_order_updated,
    notify_staff_account_created,
    notify_staff_password_reset_complete,
    send_staff_password_reset_email,
)
from .push_notifications import notify_new_reservation as notify_staff_new_reservation
from .reservation_rules import ACTIVE_RESERVATION_STATUSES, validate_lodge_stay
from .staff_api_serializers import (
    StaffAccountPasswordSerializer,
    StaffAccountSerializer,
    StaffCategorySerializer,
    StaffCreateSerializer,
    StaffCurrentPasswordSerializer,
    StaffLoginSerializer,
    StaffLodgeBlockoutSerializer,
    StaffLodgeSerializer,
    StaffOrderSerializer,
    StaffOrderUpdateSerializer,
    StaffPushSubscriptionDeleteSerializer,
    StaffPushSubscriptionSerializer,
    StaffProductSerializer,
    StaffPasswordResetConfirmSerializer,
    StaffPasswordResetRequestSerializer,
    StaffReservationCreateSerializer,
    StaffReservationSerializer,
    StaffReservationUpdateSerializer,
    StaffStatusSerializer,
    StaffUserSerializer,
)


User = get_user_model()


class StaffApiPermission(IsAdminUser):
    def has_permission(self, request, view):
        if not super().has_permission(request, view):
            return False
        needs_password_change = StaffProfile.objects.filter(
            user=request.user,
            must_change_password=True,
        ).exists()
        return not needs_password_change or getattr(
            view, "allow_password_change", False
        )


class StaffSuperuserPermission(StaffApiPermission):
    def has_permission(self, request, view):
        return super().has_permission(request, view) and request.user.is_superuser


class StaffApiMixin:
    authentication_classes = (TokenAuthentication,)
    permission_classes = (StaffApiPermission,)


class StaffReservationPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100


def validate_blockout_conflicts(lodge, start_date, end_date):
    if LodgeReservation.objects.filter(
        lodge=lodge,
        status__in=ACTIVE_RESERVATION_STATUSES,
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
        profile, _ = StaffProfile.objects.get_or_create(user=user)
        token, _ = Token.objects.get_or_create(user=user)
        return Response(
            {
                "token": token.key,
                "username": user.get_username(),
                "must_change_password": profile.must_change_password,
            }
        )


class StaffPasswordResetRequest(APIView):
    authentication_classes = ()
    permission_classes = (AllowAny,)
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = "staff_password_reset_request"

    def post(self, request):
        serializer = StaffPasswordResetRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"]
        users = User._default_manager.filter(
            email__iexact=email,
            is_staff=True,
            is_active=True,
        ).exclude(email="")
        for user in users:
            send_staff_password_reset_email(user)
        return Response(
            {
                "detail": (
                    "If an active staff account uses that email, "
                    "password reset instructions have been sent."
                )
            }
        )


class StaffPasswordResetConfirm(APIView):
    authentication_classes = ()
    permission_classes = (AllowAny,)
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = "staff_password_reset_confirm"

    @transaction.atomic
    def post(self, request):
        serializer = StaffPasswordResetConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            user_id = force_str(
                urlsafe_base64_decode(serializer.validated_data["uid"])
            )
            user = User._default_manager.get(
                pk=user_id,
                is_staff=True,
                is_active=True,
            )
        except (
            User.DoesNotExist,
            TypeError,
            ValueError,
            OverflowError,
            UnicodeDecodeError,
        ):
            return Response(
                {"detail": "This password-reset link is invalid or expired."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not default_token_generator.check_token(
            user, serializer.validated_data["token"]
        ):
            return Response(
                {"detail": "This password-reset link is invalid or expired."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        new_password = serializer.validated_data["new_password"]
        try:
            password_validation.validate_password(new_password, user)
        except DjangoValidationError as error:
            raise ValidationError({"new_password": error.messages}) from error

        user.set_password(new_password)
        user.save(update_fields=("password",))
        profile, _ = StaffProfile.objects.get_or_create(user=user)
        profile.must_change_password = False
        profile.save(update_fields=("must_change_password", "updated_at"))
        Token.objects.filter(user=user).delete()
        transaction.on_commit(
            lambda: notify_staff_password_reset_complete(user),
            robust=True,
        )
        return Response({"detail": "Your password has been reset. You can sign in."})


class StaffLogout(StaffApiMixin, APIView):
    allow_password_change = True

    def post(self, request):
        request.auth.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class StaffAccount(StaffApiMixin, APIView):
    allow_password_change = True

    def get(self, request):
        StaffProfile.objects.get_or_create(user=request.user)
        return Response(StaffAccountSerializer(request.user).data)

    def patch(self, request):
        StaffProfile.objects.get_or_create(user=request.user)
        serializer = StaffAccountSerializer(
            request.user,
            data=request.data,
            partial=True,
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class StaffAccountPasswordChange(StaffApiMixin, APIView):
    allow_password_change = True

    @transaction.atomic
    def post(self, request):
        serializer = StaffAccountPasswordSerializer(
            data=request.data,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        request.user.set_password(serializer.validated_data["new_password"])
        request.user.save(update_fields=("password",))

        profile, _ = StaffProfile.objects.get_or_create(user=request.user)
        profile.must_change_password = False
        profile.save(update_fields=("must_change_password", "updated_at"))

        Token.objects.filter(user=request.user).delete()
        token = Token.objects.create(user=request.user)
        return Response({"token": token.key})


class StaffAccountPasswordVerify(StaffApiMixin, APIView):
    allow_password_change = True

    def post(self, request):
        serializer = StaffCurrentPasswordSerializer(
            data=request.data,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        return Response(status=status.HTTP_204_NO_CONTENT)


class StaffPushVapidPublicKey(StaffApiMixin, APIView):
    def get(self, request):
        if not all(
            (
                settings.VAPID_PUBLIC_KEY,
                settings.VAPID_PRIVATE_KEY,
                settings.VAPID_SUBJECT,
            )
        ):
            return Response(
                {"detail": "Background push notifications are not configured."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response({"public_key": settings.VAPID_PUBLIC_KEY})


class StaffPushSubscriptionView(StaffApiMixin, APIView):
    def post(self, request):
        if not all(
            (
                settings.VAPID_PUBLIC_KEY,
                settings.VAPID_PRIVATE_KEY,
                settings.VAPID_SUBJECT,
            )
        ):
            return Response(
                {"detail": "Background push notifications are not configured."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        serializer = StaffPushSubscriptionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        subscription_data = serializer.validated_data
        subscription, created = StaffPushSubscription.objects.update_or_create(
            endpoint=subscription_data["endpoint"],
            defaults={
                "user": request.user,
                "p256dh": subscription_data["keys"]["p256dh"],
                "auth": subscription_data["keys"]["auth"],
            },
        )
        return Response(
            {"enabled": True},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    def delete(self, request):
        serializer = StaffPushSubscriptionDeleteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        StaffPushSubscription.objects.filter(
            user=request.user,
            endpoint=serializer.validated_data["endpoint"],
        ).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class StaffAccountListCreate(StaffApiMixin, APIView):
    permission_classes = (StaffSuperuserPermission,)

    def get(self, request):
        users = User._default_manager.filter(is_staff=True).select_related(
            "staff_profile"
        )
        return Response(StaffUserSerializer(users.order_by("username"), many=True).data)

    @transaction.atomic
    def post(self, request):
        serializer = StaffCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        temporary_password = get_random_string(24)
        user = User._default_manager.create_user(
            password=temporary_password,
            is_staff=True,
            **serializer.validated_data,
        )
        StaffProfile.objects.create(user=user, must_change_password=True)
        created_by = request.user.get_username()
        transaction.on_commit(
            lambda: notify_staff_account_created(user.get_username(), created_by),
            robust=True,
        )
        return Response(
            {
                "staff": StaffUserSerializer(user).data,
                "temporary_password": temporary_password,
            },
            status=status.HTTP_201_CREATED,
        )


class StaffAccountDetail(StaffApiMixin, APIView):
    permission_classes = (StaffSuperuserPermission,)

    @transaction.atomic
    def patch(self, request, pk):
        user = get_object_or_404(User, pk=pk, is_staff=True)
        if user.is_superuser:
            raise PermissionDenied("Superuser accounts cannot be deactivated here.")
        if user.pk == request.user.pk:
            raise PermissionDenied("You cannot deactivate your own account.")

        serializer = StaffStatusSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user.is_active = serializer.validated_data["is_active"]
        user.save(update_fields=("is_active",))
        if not user.is_active:
            Token.objects.filter(user=user).delete()
        StaffProfile.objects.get_or_create(user=user)
        return Response(StaffUserSerializer(user).data)


class StaffAccountPasswordReset(StaffApiMixin, APIView):
    permission_classes = (StaffSuperuserPermission,)

    @transaction.atomic
    def post(self, request, pk):
        user = get_object_or_404(User, pk=pk, is_staff=True)
        if user.is_superuser:
            raise PermissionDenied("Superuser passwords must be changed by their owner.")
        if user.pk == request.user.pk:
            raise PermissionDenied("Use Change password for your own account.")

        temporary_password = get_random_string(24)
        user.set_password(temporary_password)
        user.save(update_fields=("password",))

        profile, _ = StaffProfile.objects.get_or_create(user=user)
        profile.must_change_password = True
        profile.save(update_fields=("must_change_password", "updated_at"))
        Token.objects.filter(user=user).delete()
        return Response(
            {
                "staff": StaffUserSerializer(user).data,
                "temporary_password": temporary_password,
            }
        )


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

    def perform_update(self, serializer):
        order = serializer.instance
        previous_status = order.status
        previous_payment_status = order.payment_status
        updated = serializer.save()
        if (
            updated.status != previous_status
            or updated.payment_status != previous_payment_status
        ):
            transaction.on_commit(
                lambda: notify_order_updated(
                    updated,
                    previous_status,
                    previous_payment_status,
                ),
                robust=True,
            )


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
    queryset = Lodge.objects.prefetch_related("images").all()
    serializer_class = StaffLodgeSerializer


class StaffLodgeDetail(StaffApiMixin, generics.RetrieveUpdateAPIView):
    queryset = Lodge.objects.prefetch_related("images").all()
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
        transaction.on_commit(
            lambda: notify_new_reservation(reservation),
            robust=True,
        )
        if reservation.status == LodgeReservation.Status.PENDING:
            transaction.on_commit(
                lambda: notify_staff_new_reservation(reservation),
                robust=True,
            )


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
            transaction.on_commit(
                lambda: notify_reservation_status(updated),
                robust=True,
            )

    def perform_destroy(self, instance):
        if instance.status in (
            LodgeReservation.Status.PENDING,
            LodgeReservation.Status.CONFIRMED,
            LodgeReservation.Status.CHECKED_IN,
        ):
            raise ValidationError(
                {"status": "Complete or cancel an active reservation before deleting it."}
            )
        instance.delete()
