from datetime import date, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.authtoken.models import Token
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

    def test_customer_can_track_order_with_number_and_phone_without_private_fields(self):
        order_response = self.client.post(
            "/api/v1/orders/",
            {
                "guest_name": "A Guest",
                "phone": "+256700000000",
                "order_type": "dine_in",
                "location": "Table 4",
                "items": [{"product": self.product.slug, "quantity": 2}],
            },
            format="json",
        )

        response = self.client.post(
            "/api/v1/orders/track/",
            {
                "number": order_response.data["number"].lower(),
                "phone": "+256700000000",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], Order.Status.NEW)
        self.assertEqual(response.data["status_label"], "New")
        self.assertEqual(response.data["items"][0]["product_name"], self.product.name)
        self.assertNotIn("phone", response.data)
        self.assertNotIn("guest_name", response.data)

    def test_customer_order_tracking_requires_matching_phone(self):
        order_response = self.client.post(
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

        response = self.client.post(
            "/api/v1/orders/track/",
            {"number": order_response.data["number"], "phone": "+256700000001"},
            format="json",
        )

        self.assertEqual(response.status_code, 404)

    def test_customer_tracking_reflects_staff_order_and_payment_updates(self):
        order_response = self.client.post(
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
        staff_user = get_user_model().objects.create_user(
            username="order-staff", password="staff-password", is_staff=True
        )
        token = Token.objects.create(user=staff_user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")

        status_response = self.client.patch(
            f"/api/v1/staff/orders/{Order.objects.get(number=order_response.data['number']).id}/",
            {"status": Order.Status.ACCEPTED},
            format="json",
        )
        payment_response = self.client.patch(
            f"/api/v1/staff/orders/{Order.objects.get(number=order_response.data['number']).id}/",
            {"payment_status": Order.PaymentStatus.PAID},
            format="json",
        )
        tracking_response = self.client.post(
            "/api/v1/orders/track/",
            {"number": order_response.data["number"], "phone": "+256700000000"},
            format="json",
        )

        self.assertEqual(status_response.status_code, 200)
        self.assertEqual(payment_response.status_code, 200)
        self.assertEqual(tracking_response.status_code, 200)
        self.assertEqual(tracking_response.data["status"], Order.Status.ACCEPTED)
        self.assertEqual(tracking_response.data["status_label"], "Confirmed")
        self.assertEqual(tracking_response.data["payment_status"], Order.PaymentStatus.PAID)

    def test_staff_cannot_skip_order_statuses_or_revert_payment(self):
        staff_user = get_user_model().objects.create_user(
            username="restricted-order-staff",
            password="staff-password",
            is_staff=True,
        )
        token = Token.objects.create(user=staff_user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        order = Order.objects.create(
            guest_name="Order Guest",
            phone="+256700000000",
            order_type=Order.OrderType.DINE_IN,
            location="Table 2",
        )

        skip_response = self.client.patch(
            f"/api/v1/staff/orders/{order.id}/",
            {"status": Order.Status.COMPLETED},
            format="json",
        )
        paid_response = self.client.patch(
            f"/api/v1/staff/orders/{order.id}/",
            {"status": Order.Status.ACCEPTED},
            format="json",
        )
        paid_order_response = self.client.patch(
            f"/api/v1/staff/orders/{order.id}/",
            {"payment_status": Order.PaymentStatus.PAID},
            format="json",
        )
        revert_response = self.client.patch(
            f"/api/v1/staff/orders/{order.id}/",
            {"payment_status": Order.PaymentStatus.UNPAID},
            format="json",
        )

        self.assertEqual(skip_response.status_code, 400)
        self.assertEqual(paid_response.status_code, 200)
        self.assertEqual(paid_order_response.status_code, 200)
        self.assertEqual(revert_response.status_code, 400)

        cancelled_order = Order.objects.create(
            guest_name="Cancelled Guest",
            phone="+256700000003",
            order_type=Order.OrderType.DINE_IN,
            location="Table 3",
            status=Order.Status.CANCELLED,
        )
        cancelled_payment_response = self.client.patch(
            f"/api/v1/staff/orders/{cancelled_order.id}/",
            {"payment_status": Order.PaymentStatus.PAID},
            format="json",
        )
        self.assertEqual(cancelled_payment_response.status_code, 400)

    def test_public_order_is_visible_to_staff_orders_api(self):
        order_response = self.client.post(
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
        self.assertEqual(order_response.status_code, 201)

        staff_user = get_user_model().objects.create_user(
            username="staff", password="staff-password", is_staff=True
        )
        token = Token.objects.create(user=staff_user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")

        staff_response = self.client.get("/api/v1/staff/orders/")

        self.assertEqual(staff_response.status_code, 200)
        self.assertEqual(staff_response.data[0]["number"], order_response.data["number"])

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


class StaffApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.staff = get_user_model().objects.create_user(
            username="portal-admin",
            password="staff-password",
            email="admin@example.com",
            is_staff=True,
        )
        self.regular_user = get_user_model().objects.create_user(
            username="guest",
            password="guest-password",
        )
        self.category = Category.objects.create(name="Drinks")
        self.lodge = Lodge.objects.create(name="View Cottage", capacity=4)
        self.product = Product.objects.create(
            name="Resort Juice",
            category=self.category,
            price_ugx=5000,
        )

    def login_as_staff(self):
        response = self.client.post(
            "/api/v1/auth/token/",
            {"username": "portal-admin", "password": "staff-password"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Token {response.data['token']}"
        )

    def test_staff_login_rejects_non_staff_users(self):
        response = self.client.post(
            "/api/v1/auth/token/",
            {"username": "guest", "password": "guest-password"},
            format="json",
        )
        self.assertEqual(response.status_code, 401)

    def test_staff_endpoints_require_staff_token(self):
        response = self.client.get("/api/v1/staff/products/")
        self.assertEqual(response.status_code, 401)

        self.client.force_authenticate(user=self.regular_user)
        response = self.client.get("/api/v1/staff/products/")
        self.assertEqual(response.status_code, 403)

    def test_staff_can_create_and_edit_products(self):
        self.login_as_staff()
        response = self.client.post(
            "/api/v1/staff/products/",
            {
                "name": "Lake Smoothie",
                "category_id": self.category.id,
                "description": "Fresh fruit",
                "price_ugx": 8000,
                "image_url": "",
                "is_available": True,
                "is_featured": False,
                "sort_order": 0,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        product_id = response.data["id"]
        self.assertEqual(response.data["category_id"], self.category.id)

        response = self.client.patch(
            f"/api/v1/staff/products/{product_id}/",
            {"price_ugx": 9000},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["price_ugx"], 9000)

    def test_staff_can_upload_product_image_for_public_menu(self):
        self.login_as_staff()
        with (
            override_settings(
                CLOUDINARY_NAME="demo",
                CLOUDINARY_KEY="test-key",
                CLOUDINARY_SECRET="test-secret",
            ),
            patch(
                "core.staff_api_serializers.cloudinary.uploader.upload",
                side_effect=lambda image, **options: {
                    "secure_url": (
                        "https://res.cloudinary.com/demo/image/upload/"
                        f"{options['public_id']}.webp"
                    ),
                    "public_id": options["public_id"],
                },
            ) as upload_image,
        ):
            response = self.client.post(
                "/api/v1/staff/products/",
                {
                    "name": "Uploaded Dish",
                    "category_id": self.category.id,
                    "description": "",
                    "price_ugx": 8000,
                    "image_url": "",
                    "image_file": SimpleUploadedFile(
                        "dish.webp", b"sample image", content_type="image/webp"
                    ),
                    "is_available": True,
                    "is_featured": False,
                    "sort_order": 0,
                },
                format="multipart",
            )
        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.data["image_url"].startswith("https://res.cloudinary.com/"))
        upload_image.assert_called_once()

        public_products = self.client.get("/api/v1/products/")
        uploaded_product = next(
            product
            for product in public_products.data
            if product["id"] == response.data["id"]
        )
        self.assertEqual(uploaded_product["image"], response.data["image_url"])

    def test_staff_product_image_rejects_data_urls(self):
        self.login_as_staff()
        response = self.client.post(
            "/api/v1/staff/products/",
            {
                "name": "Invalid Image Dish",
                "category_id": self.category.id,
                "description": "",
                "price_ugx": 8000,
                "image_url": "data:text/html;base64,PGgxPk5vdCBhbiBpbWFnZTwvaDE+",
                "is_available": True,
                "is_featured": False,
                "sort_order": 0,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("image_url", response.data)

    def test_staff_product_image_rejects_oversized_uploads(self):
        self.login_as_staff()
        response = self.client.post(
            "/api/v1/staff/products/",
            {
                "name": "Oversized Image Dish",
                "category_id": self.category.id,
                "description": "",
                "price_ugx": 8000,
                "image_file": SimpleUploadedFile(
                    "large.webp",
                    b"x" * (1_572_864 + 1),
                    content_type="image/webp",
                ),
                "is_available": True,
                "is_featured": False,
                "sort_order": 0,
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("image_file", response.data)

    def test_product_image_upload_requires_cloudinary_configuration(self):
        self.login_as_staff()
        with override_settings(
            CLOUDINARY_NAME="",
            CLOUDINARY_KEY="",
            CLOUDINARY_SECRET="",
        ):
            response = self.client.post(
                "/api/v1/staff/products/",
                {
                    "name": "Unconfigured Image Dish",
                    "category_id": self.category.id,
                    "description": "",
                    "price_ugx": 8000,
                    "image_file": SimpleUploadedFile(
                        "dish.webp", b"sample image", content_type="image/webp"
                    ),
                    "is_available": True,
                    "is_featured": False,
                    "sort_order": 0,
                },
                format="multipart",
            )
        self.assertEqual(response.status_code, 400)
        self.assertIn("image_file", response.data)
        self.assertFalse(Product.objects.filter(name="Unconfigured Image Dish").exists())

    def test_staff_can_accept_orders_and_conflicting_reservations_cannot_be_confirmed(self):
        self.login_as_staff()
        order = Order.objects.create(
            guest_name="Order Guest",
            phone="+256700000000",
            order_type=Order.OrderType.DINE_IN,
            location="Table 2",
        )
        response = self.client.patch(
            f"/api/v1/staff/orders/{order.id}/",
            {"status": Order.Status.ACCEPTED},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.ACCEPTED)
        for next_status in (
            Order.Status.PREPARING,
            Order.Status.READY,
            Order.Status.COMPLETED,
        ):
            response = self.client.patch(
                f"/api/v1/staff/orders/{order.id}/",
                {"status": next_status},
                format="json",
            )
            self.assertEqual(response.status_code, 200)
            order.refresh_from_db()
            self.assertEqual(order.status, next_status)

        today = date.today()
        LodgeReservation.objects.create(
            lodge=self.lodge,
            guest_name="Confirmed Guest",
            phone="+256700000001",
            check_in=today + timedelta(days=4),
            check_out=today + timedelta(days=7),
            status=LodgeReservation.Status.CONFIRMED,
        )
        pending = LodgeReservation.objects.create(
            lodge=self.lodge,
            guest_name="Pending Guest",
            phone="+256700000002",
            check_in=today + timedelta(days=6),
            check_out=today + timedelta(days=8),
        )
        response = self.client.patch(
            f"/api/v1/staff/reservations/{pending.id}/",
            {"status": LodgeReservation.Status.CONFIRMED},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        pending.refresh_from_db()
        self.assertEqual(pending.status, LodgeReservation.Status.PENDING)

    def test_staff_can_manage_categories_and_lodges(self):
        self.login_as_staff()
        category_response = self.client.post(
            "/api/v1/staff/categories/",
            {"name": "Breakfast", "description": "Morning menu", "sort_order": 1},
            format="json",
        )
        self.assertEqual(category_response.status_code, 201)

        lodge_response = self.client.post(
            "/api/v1/staff/lodges/",
            {
                "name": "Forest Cabin",
                "description": "Quiet cabin",
                "capacity": 3,
                "image_url": "",
                "is_active": True,
            },
            format="json",
        )
        self.assertEqual(lodge_response.status_code, 201)
