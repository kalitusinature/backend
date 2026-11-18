from django.urls import path

from .staff_views import (
    StaffLoginView,
    StaffLogoutView,
    dashboard,
    update_order_status,
    update_reservation_status,
)

urlpatterns = [
    path("login/", StaffLoginView.as_view(), name="staff_login"),
    path("logout/", StaffLogoutView.as_view(), name="staff_logout"),
    path("", dashboard, name="staff_dashboard"),
    path("orders/<int:order_id>/status/", update_order_status, name="staff_order_status"),
    path(
        "reservations/<int:reservation_id>/status/",
        update_reservation_status,
        name="staff_reservation_status",
    ),
]
