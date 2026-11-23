import html

from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils.dateparse import parse_date
from django.utils import timezone
from rest_framework import generics, status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.renderers import BaseRenderer, JSONRenderer
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from .email_notifications import send_payment_receipt_email
from .models import LodgeReservation, Order, Payment, PaymentReceipt
from .payment_receipts import create_payment_receipt
from .serializers import (
    CustomerPaymentReceiptSerializer,
    OrderReceiptDownloadRequestSerializer,
    OrderReceiptLookupRequestSerializer,
    ReservationReceiptDownloadRequestSerializer,
    ReservationTrackingRequestSerializer,
    ReservationTrackingSerializer,
)
from .staff_api_serializers import (
    StaffPaymentCreateSerializer,
    StaffPaymentReceiptSerializer,
)
from .staff_api_views import StaffApiMixin


class ReceiptVerificationHTMLRenderer(BaseRenderer):
    media_type = "text/html"
    format = "html"
    charset = "utf-8"

    def render(self, data, accepted_media_type=None, renderer_context=None):
        if data.get("status") == "verified":
            receipt_number = html.escape(str(data["receipt_number"]))
            amount = f"UGX {int(data['amount_ugx']):,}"
            issued_at = timezone.localtime(data["issued_at"])
            issued = (
                f"{issued_at.day} "
                f"{issued_at.strftime('%B %Y at %I:%M %p %Z')}"
            )
            content = f"""
                <span class="badge">Verified</span>
                <h1>Payment receipt verified</h1>
                <p class="intro">This payment receipt is valid and was issued by Kalitusi Nature Resort.</p>
                <dl>
                    <div><dt>Receipt number</dt><dd>{receipt_number}</dd></div>
                    <div><dt>Amount paid</dt><dd>{amount}</dd></div>
                    <div><dt>Issued</dt><dd>{html.escape(issued)}</dd></div>
                </dl>
                <p class="footnote">This page confirms receipt authenticity. It does not show customer or transaction details.</p>
            """
        else:
            content = """
                <span class="badge invalid">Not verified</span>
                <h1>Receipt not found</h1>
                <p class="intro">We could not verify this receipt. Please check the QR code or contact Kalitusi Nature Resort.</p>
            """
        return f"""<!doctype html>
            <html lang="en">
            <head>
                <meta charset="utf-8">
                <meta name="viewport" content="width=device-width, initial-scale=1">
                <meta name="robots" content="noindex, nofollow">
                <title>Receipt verification | Kalitusi Nature Resort</title>
                <style>
                    * {{ box-sizing: border-box; }}
                    body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 24px; background: #f2f6f2; color: #23332b; font-family: Arial, sans-serif; }}
                    main {{ width: min(100%, 520px); padding: 36px; border: 1px solid #dce6e0; border-radius: 16px; background: #fff; box-shadow: 0 12px 36px rgba(23, 75, 58, .08); }}
                    .brand {{ margin-bottom: 28px; color: #174b3a; font-size: 15px; font-weight: 700; letter-spacing: .04em; }}
                    .badge {{ display: inline-block; padding: 7px 12px; border-radius: 999px; background: #e7f4ec; color: #17613b; font-size: 13px; font-weight: 700; }}
                    .badge.invalid {{ background: #fff0ed; color: #a13d2a; }}
                    h1 {{ margin: 18px 0 8px; color: #174b3a; font-size: clamp(24px, 6vw, 32px); }}
                    .intro {{ margin: 0 0 24px; color: #58675f; line-height: 1.6; }}
                    dl {{ margin: 0; border-top: 1px solid #e4ebe6; }}
                    dl div {{ display: flex; justify-content: space-between; gap: 16px; padding: 15px 0; border-bottom: 1px solid #e4ebe6; }}
                    dt {{ color: #63746c; }}
                    dd {{ margin: 0; text-align: right; font-weight: 700; overflow-wrap: anywhere; }}
                    .footnote {{ margin: 20px 0 0; color: #76847c; font-size: 13px; line-height: 1.5; }}
                    @media (max-width: 480px) {{ main {{ padding: 26px 22px; }} dl div {{ display: block; }} dd {{ margin-top: 5px; text-align: left; }} }}
                </style>
            </head>
            <body>
                <main>
                    <div class="brand">KALITUSI NATURE RESORT</div>
                    {content}
                </main>
            </body>
            </html>"""


class StaffPaymentPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100


def _verified_total(target) -> int:
    return (
        target.payments.filter(status=Payment.Status.VERIFIED).aggregate(
            total=Sum("amount_ugx")
        )["total"]
        or 0
    )


def _order_paid_total(order: Order) -> int:
    total = _verified_total(order)
    if total == 0 and order.payment_status == Order.PaymentStatus.PAID:
        return order.total_ugx
    return total


def _pdf_response(receipt: PaymentReceipt) -> HttpResponse:
    response = HttpResponse(bytes(receipt.pdf_data), content_type="application/pdf")
    response["Content-Disposition"] = (
        f'attachment; filename="{receipt.number}.pdf"'
    )
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


class StaffPaymentCreate(StaffApiMixin, APIView):
    def post(self, request):
        serializer = StaffPaymentCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        transaction_reference = data["transaction_reference"]
        try:
            with transaction.atomic():
                if Payment.objects.filter(
                    transaction_reference__iexact=transaction_reference
                ).exists():
                    raise ValidationError(
                        {
                            "transaction_reference": (
                                "This transaction reference has already been recorded."
                            )
                        }
                    )

                order = None
                reservation = None
                if data.get("order_id"):
                    order = get_object_or_404(
                        Order.objects.select_for_update(),
                        pk=data["order_id"],
                    )
                    if order.status == Order.Status.CANCELLED:
                        raise ValidationError(
                            {"order_id": "A cancelled order cannot receive a payment."}
                        )
                    total_due = order.total_ugx
                    paid = _order_paid_total(order)
                    if total_due <= 0:
                        raise ValidationError(
                            {"order_id": "This order has no payable balance."}
                        )
                else:
                    reservation = get_object_or_404(
                        LodgeReservation.objects.select_for_update(),
                        pk=data["reservation_id"],
                    )
                    if reservation.status in (
                        LodgeReservation.Status.DECLINED,
                        LodgeReservation.Status.CANCELLED,
                    ):
                        raise ValidationError(
                            {
                                "reservation_id": (
                                    "A rejected or cancelled reservation cannot receive a payment."
                                )
                            }
                        )
                    total_due = reservation.total_due_ugx
                    paid = _verified_total(reservation)
                    if total_due <= 0:
                        raise ValidationError(
                            {
                                "reservation_id": (
                                    "Set the reservation total before recording a payment."
                                )
                            }
                        )

                balance = max(total_due - paid, 0)
                if data["amount_ugx"] > balance:
                    raise ValidationError(
                        {
                            "amount_ugx": (
                                f"Payment exceeds the outstanding balance of UGX {balance:,}."
                            )
                        }
                    )
                if balance == 0:
                    raise ValidationError(
                        {"amount_ugx": "There is no outstanding balance to pay."}
                    )

                payment = Payment.objects.create(
                    order=order,
                    reservation=reservation,
                    amount_ugx=data["amount_ugx"],
                    method=data["method"],
                    transaction_reference=transaction_reference,
                    status=Payment.Status.VERIFIED,
                    verified_by=request.user,
                )
                receipt = create_payment_receipt(payment)

                paid_to_date = paid + payment.amount_ugx
                if order:
                    order.payment_status = (
                        Order.PaymentStatus.PAID
                        if paid_to_date == total_due
                        else Order.PaymentStatus.PARTIAL
                    )
                    order.save(update_fields=["payment_status", "updated_at"])
        except IntegrityError:
            if Payment.objects.filter(
                transaction_reference__iexact=transaction_reference
            ).exists():
                raise ValidationError(
                    {
                        "transaction_reference": (
                            "This transaction reference has already been recorded."
                        )
                    }
                )
            raise

        email_sent = send_payment_receipt_email(payment)
        payment = (
            Payment.objects.select_related(
                "order",
                "reservation",
                "reservation__lodge",
                "receipt",
            )
            .get(pk=payment.pk)
        )
        response_data = dict(StaffPaymentReceiptSerializer(payment).data)
        response_data["email_available"] = bool(
            (payment.order or payment.reservation).email
        )
        response_data["email_sent"] = email_sent
        return Response(response_data, status=status.HTTP_201_CREATED)


class StaffPaymentReceiptList(StaffApiMixin, generics.ListAPIView):
    serializer_class = StaffPaymentReceiptSerializer
    pagination_class = StaffPaymentPagination

    def get_queryset(self):
        queryset = Payment.objects.filter(
            status=Payment.Status.VERIFIED,
            receipt__isnull=False,
        ).select_related(
            "order",
            "reservation",
            "reservation__lodge",
            "receipt",
        )
        query = self.request.query_params.get("q", "").strip()
        if query:
            queryset = queryset.filter(
                Q(receipt__number__icontains=query)
                | Q(transaction_reference__icontains=query)
                | Q(order__number__icontains=query)
                | Q(order__guest_name__icontains=query)
                | Q(reservation__reference__icontains=query)
                | Q(reservation__guest_name__icontains=query)
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
            raise ValidationError(
                {"end_date": "End date must not be before start date."}
            )
        if start:
            queryset = queryset.filter(verified_at__date__gte=start)
        if end:
            queryset = queryset.filter(verified_at__date__lte=end)

        payment_status = self.request.query_params.get("status", "all")
        if payment_status == Order.PaymentStatus.PAID:
            queryset = queryset.filter(receipt__balance_ugx=0)
        elif payment_status == Order.PaymentStatus.PARTIAL:
            queryset = queryset.filter(receipt__balance_ugx__gt=0)
        elif payment_status != "all":
            raise ValidationError({"status": "Use all, partial, or paid."})
        return queryset.order_by("-verified_at", "-id")


class StaffPaymentReceiptDownload(StaffApiMixin, APIView):
    def get(self, request, pk):
        receipt = get_object_or_404(
            PaymentReceipt.objects.select_related("payment"),
            payment_id=pk,
        )
        return _pdf_response(receipt)


class StaffPaymentReceiptResend(StaffApiMixin, APIView):
    def post(self, request, pk):
        payment = get_object_or_404(
            Payment.objects.select_related(
                "order",
                "reservation",
                "reservation__lodge",
                "receipt",
            ),
            pk=pk,
            status=Payment.Status.VERIFIED,
        )
        target = payment.order or payment.reservation
        if not target.email:
            return Response(
                {"detail": "The customer has no email address on file."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not send_payment_receipt_email(payment):
            return Response(
                {"detail": "Receipt email delivery failed. Check email settings and retry."},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response({"detail": "Payment receipt sent."})


class CustomerReceiptLookupMixin:
    authentication_classes = ()
    permission_classes = (AllowAny,)
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = "receipt_lookup"


class OrderReceiptList(CustomerReceiptLookupMixin, APIView):
    def post(self, request):
        serializer = OrderReceiptLookupRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        order = get_object_or_404(
            Order.objects.prefetch_related("payments__receipt"),
            number__iexact=serializer.validated_data["number"].strip(),
            email__iexact=serializer.validated_data["email"],
        )
        payments = [
            payment
            for payment in order.payments.all()
            if payment.status == Payment.Status.VERIFIED
            and hasattr(payment, "receipt")
        ]
        return Response(
            {
                "number": order.number,
                "receipts": CustomerPaymentReceiptSerializer(
                    payments,
                    many=True,
                ).data,
            }
        )


class OrderReceiptDownload(CustomerReceiptLookupMixin, APIView):
    def post(self, request):
        serializer = OrderReceiptDownloadRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        order = get_object_or_404(
            Order,
            number__iexact=data["number"].strip(),
            email__iexact=data["email"],
        )
        receipt = get_object_or_404(
            PaymentReceipt,
            payment__order=order,
            payment__status=Payment.Status.VERIFIED,
            number__iexact=data["receipt_number"].strip(),
        )
        return _pdf_response(receipt)


class ReservationTrack(CustomerReceiptLookupMixin, APIView):
    def post(self, request):
        serializer = ReservationTrackingRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        reservation = get_object_or_404(
            LodgeReservation.objects.select_related("lodge").prefetch_related(
                "payments__receipt"
            ),
            reference__iexact=serializer.validated_data["reference"].strip(),
            email__iexact=serializer.validated_data["email"],
        )
        return Response(ReservationTrackingSerializer(reservation).data)


class ReservationReceiptDownload(CustomerReceiptLookupMixin, APIView):
    def post(self, request):
        serializer = ReservationReceiptDownloadRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        reservation = get_object_or_404(
            LodgeReservation,
            reference__iexact=data["reference"].strip(),
            email__iexact=data["email"],
        )
        receipt = get_object_or_404(
            PaymentReceipt,
            payment__reservation=reservation,
            payment__status=Payment.Status.VERIFIED,
            number__iexact=data["receipt_number"].strip(),
        )
        return _pdf_response(receipt)


class ReceiptVerification(APIView):
    authentication_classes = ()
    permission_classes = (AllowAny,)
    renderer_classes = (JSONRenderer, ReceiptVerificationHTMLRenderer)
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = "receipt_verification"

    def get(self, request, token):
        from .payment_receipts import find_verified_receipt

        receipt = find_verified_receipt(token)
        if receipt is None:
            response = Response(
                {"detail": "Receipt not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        else:
            response = Response(
                {
                    "receipt_number": receipt.number,
                    "status": "verified",
                    "amount_ugx": receipt.payment.amount_ugx,
                    "issued_at": receipt.payment.verified_at,
                }
            )
        response["Cache-Control"] = "no-store"
        response["X-Robots-Tag"] = "noindex, nofollow"
        return response
