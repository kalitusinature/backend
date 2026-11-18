from django.urls import path

from .api_views import CategoryList, LodgeList, OrderCreate, ProductList, ReservationCreate

urlpatterns = [
    path("categories/", CategoryList.as_view(), name="api_categories"),
    path("products/", ProductList.as_view(), name="api_products"),
    path("lodges/", LodgeList.as_view(), name="api_lodges"),
    path("orders/", OrderCreate.as_view(), name="api_orders"),
    path("reservations/", ReservationCreate.as_view(), name="api_reservations"),
]
