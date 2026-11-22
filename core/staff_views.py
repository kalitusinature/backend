from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.views import LoginView, LogoutView
from django.db import transaction
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .models import Category, Lodge, LodgeReservation, Order, Product
from .reservation_rules import ACTIVE_RESERVATION_STATUSES
from .reservation_notifications import notify_reservation_status


class StaffLoginView(LoginView):
    template_name = "registration/login.html"
    redirect_authenticated_user = True


class StaffLogoutView(LogoutView):
    next_page = "staff_login"


@staff_member_required(login_url="staff_login")
def dashboard(request):
    context = {
        "order_count": Order.objects.exclude(status=Order.Status.CANCELLED).count(),
        "new_order_count": Order.objects.filter(status=Order.Status.NEW).count(),
        "pending_reservation_count": LodgeReservation.objects.filter(
            status=LodgeReservation.Status.PENDING
        ).count(),
        "product_count": Product.objects.count(),
        "category_count": Category.objects.count(),
        "lodge_count": Lodge.objects.count(),
        "orders": Order.objects.select_related().prefetch_related("items")[:12],
        "reservations": LodgeReservation.objects.select_related("lodge")[:12],
        "order_statuses": Order.Status.choices,
        "reservation_statuses": LodgeReservation.Status.choices,
    }
    return render(request, "staff/dashboard.html", context)


@staff_member_required(login_url="staff_login")
@require_POST
def update_order_status(request, order_id):
    order = get_object_or_404(Order, pk=order_id)
    status_value = request.POST.get("status")
    if status_value not in Order.Status.values:
        return HttpResponseBadRequest("Invalid order status.")
    order.status = status_value
    order.save(update_fields=["status", "updated_at"])
    messages.success(request, f"Order {order.number} updated.")
    return redirect("staff_dashboard")


@staff_member_required(login_url="staff_login")
@require_POST
@transaction.atomic
def update_reservation_status(request, reservation_id):
    reservation = get_object_or_404(
        LodgeReservation.objects.select_for_update().select_related("lodge"),
        pk=reservation_id,
    )
    status_value = request.POST.get("status")
    if status_value not in LodgeReservation.Status.values:
        return HttpResponseBadRequest("Invalid reservation status.")
    if status_value in (
        LodgeReservation.Status.CONFIRMED,
        LodgeReservation.Status.CHECKED_IN,
    ):
        Lodge.objects.select_for_update().get(pk=reservation.lodge_id)
        conflict = LodgeReservation.objects.filter(
            lodge=reservation.lodge,
            status__in=ACTIVE_RESERVATION_STATUSES,
            check_in__lt=reservation.check_out,
            check_out__gt=reservation.check_in,
        ).exclude(pk=reservation.pk).exists()
        if conflict:
            messages.error(
                request,
                "Cannot confirm or check in: this lodge already has an active reservation for overlapping dates.",
            )
            return redirect("staff_dashboard")
    previous_status = reservation.status
    reservation.status = status_value
    reservation.save(update_fields=["status"])
    if status_value != previous_status:
        transaction.on_commit(
            lambda: notify_reservation_status(reservation),
            robust=True,
        )
    messages.success(request, f"Reservation {reservation.reference} updated.")
    return redirect("staff_dashboard")
