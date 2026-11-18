from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from .models import (
    Category,
    Lodge,
    LodgeReservation,
    Order,
    Product,
)


class PublicOrderingApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.category = Category.objects.create(name="Burgers")
        self.product = Product.objects.create(
            name="Resort Burger",
            category=self.category,
            price_ugx=12000,
            is_available=True,
        )

    def test_order_uses_catalog_price_and_creates_unpaid_order(self):
        response = self.client.post(
            "/api/v1/orders/",
            {
                "guest_name": "A Guest",
                "phone": "+256700000000",
                "order_type": "dine_in",
                "location": "Table 4",
                "payment_method": "cash",
                "items": [{"product": self.product.slug, "quantity": 2}],
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        order = Order.objects.get(number=response.data["number"])
        self.assertEqual(order.total_ugx, 24000)
        self.assertEqual(order.payment_status, Order.PaymentStatus.UNPAID)
        self.assertEqual(order.items.get().line_total_ugx, 24000)

    def test_unavailable_product_cannot_be_ordered(self):
        self.product.is_available = False
        self.product.save()
        response = self.client.post(
            "/api/v1/orders/",
            {
                "guest_name": "A Guest",
                "phone": "+256700000000",
                "order_type": "dine_in",
                "location": "Table 4",
                "items": [{"product": self.product.slug, "quantity": 1}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Order.objects.count(), 0)

    def test_reservation_is_pending_and_validates_lodge_capacity(self):
        lodge = Lodge.objects.create(name="Lake Cottage", capacity=3)
        response = self.client.post(
            "/api/v1/reservations/",
            {
                "lodge": lodge.slug,
                "guest_name": "A Guest",
                "phone": "+256700000000",
                "check_in": "2027-01-10",
                "check_out": "2027-01-12",
                "adults": 2,
                "children": 1,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        reservation = LodgeReservation.objects.get(reference=response.data["reference"])
        self.assertEqual(reservation.status, LodgeReservation.Status.PENDING)

        response = self.client.post(
            "/api/v1/reservations/",
            {
                "lodge": lodge.slug,
                "guest_name": "Another Guest",
                "phone": "+256700000001",
                "check_in": "2027-01-10",
                "check_out": "2027-01-11",
                "adults": 4,
                "children": 0,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)


class StaffReservationApprovalTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.staff = user_model.objects.create_user(
            username="staff", password="test-password", is_staff=True
        )
        self.lodge = Lodge.objects.create(name="Garden Lodge", capacity=4)
        today = date.today()
        self.first = LodgeReservation.objects.create(
            lodge=self.lodge,
            guest_name="First Guest",
            phone="+256700000000",
            check_in=today + timedelta(days=4),
            check_out=today + timedelta(days=7),
        )
        self.second = LodgeReservation.objects.create(
            lodge=self.lodge,
            guest_name="Second Guest",
            phone="+256700000001",
            check_in=today + timedelta(days=6),
            check_out=today + timedelta(days=8),
        )

    def test_staff_cannot_confirm_overlapping_reservation(self):
        self.first.status = LodgeReservation.Status.CONFIRMED
        self.first.save()
        self.client.force_login(self.staff)
        response = self.client.post(
            reverse("staff_reservation_status", args=[self.second.pk]),
            {"status": LodgeReservation.Status.CONFIRMED},
        )
        self.second.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.second.status, LodgeReservation.Status.PENDING)
