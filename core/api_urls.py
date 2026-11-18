from django.urls import path

from .api_views import (
    CategoryList,
    LodgeList,
    OrderCreate,
    OrderTrack,
    ProductList,
    ReservationCreate,
)
from .staff_api_views import (
    StaffCategoryDetail,
    StaffCategoryListCreate,
    StaffDashboard,
    StaffLodgeDetail,
    StaffLodgeListCreate,
    StaffLogin,
    StaffLogout,
    StaffOrderList,
    StaffOrderUpdate,
    StaffProductDetail,
    StaffProductListCreate,
    StaffReservationList,
    StaffReservationUpdate,
)

urlpatterns = [
    path("auth/token/", StaffLogin.as_view(), name="staff_api_login"),
    path("auth/logout/", StaffLogout.as_view(), name="staff_api_logout"),
    path("categories/", CategoryList.as_view(), name="api_categories"),
    path("products/", ProductList.as_view(), name="api_products"),
    path("lodges/", LodgeList.as_view(), name="api_lodges"),
    path("orders/", OrderCreate.as_view(), name="api_orders"),
    path("orders/track/", OrderTrack.as_view(), name="api_order_track"),
    path("reservations/", ReservationCreate.as_view(), name="api_reservations"),
    path("staff/dashboard/", StaffDashboard.as_view(), name="staff_api_dashboard"),
    path("staff/orders/", StaffOrderList.as_view(), name="staff_api_orders"),
    path(
        "staff/orders/<int:pk>/",
        StaffOrderUpdate.as_view(),
        name="staff_api_order_update",
    ),
    path(
        "staff/products/",
        StaffProductListCreate.as_view(),
        name="staff_api_products",
    ),
    path(
        "staff/products/<int:pk>/",
        StaffProductDetail.as_view(),
        name="staff_api_product_detail",
    ),
    path(
        "staff/categories/",
        StaffCategoryListCreate.as_view(),
        name="staff_api_categories",
    ),
    path(
        "staff/categories/<int:pk>/",
        StaffCategoryDetail.as_view(),
        name="staff_api_category_detail",
    ),
    path(
        "staff/lodges/",
        StaffLodgeListCreate.as_view(),
        name="staff_api_lodges",
    ),
    path(
        "staff/lodges/<int:pk>/",
        StaffLodgeDetail.as_view(),
        name="staff_api_lodge_detail",
    ),
    path(
        "staff/reservations/",
        StaffReservationList.as_view(),
        name="staff_api_reservations",
    ),
    path(
        "staff/reservations/<int:pk>/",
        StaffReservationUpdate.as_view(),
        name="staff_api_reservation_update",
    ),
]
