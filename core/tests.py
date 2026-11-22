import json
from datetime import date, timedelta
from io import BytesIO
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
import uuid

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from .models import (
    Category,
    GuestCart,
    Lodge,
    LodgeBlockout,
    LodgeReservation,
    Order,
    OrderItem,
    Product,
    StaffProfile,
    StaffPushSubscription,
)
from .email_notifications import (
    notify_order_created,
    notify_order_updated,
    send_staff_password_reset_email,
)
from .push_notifications import notify_new_order


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    RESEND_API_KEY="",
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

    def test_backend_reservation_page_renders_with_lodge_preselection_support(self):
        response = self.client.get(
            "/reservation/",
            {"lodge": "lake-cottage"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Request a cottage stay")
        self.assertContains(response, 'id="reservation-form"')
        self.assertContains(response, "new URLSearchParams(window.location.search)")

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

    @patch("core.api_views.notify_new_order")
    def test_new_order_schedules_staff_push_after_commit(self, push_order):
        with self.captureOnCommitCallbacks(execute=True):
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

        self.assertEqual(response.status_code, 201)
        push_order.assert_called_once()
        self.assertEqual(push_order.call_args.args[0].number, response.data["number"])

    def test_order_creation_succeeds_when_notification_callbacks_fail(self):
        with (
            self.captureOnCommitCallbacks(execute=True),
            patch(
                "core.api_views.notify_new_order",
                side_effect=ValueError("Invalid test push key"),
            ),
            patch(
                "core.api_views.notify_order_created",
                side_effect=RuntimeError("Email provider unavailable"),
            ),
        ):
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

        self.assertEqual(response.status_code, 201)
        self.assertTrue(Order.objects.filter(number=response.data["number"]).exists())

    @override_settings(
        VAPID_PUBLIC_KEY="public-key",
        VAPID_PRIVATE_KEY="private-key",
        VAPID_SUBJECT="mailto:admin@kalitusi.com",
    )
    @patch("core.push_notifications.webpush")
    def test_new_order_push_targets_all_active_staff_subscriptions(self, send_push):
        staff_model = get_user_model()
        active_staff = staff_model.objects.create_user(
            username="active-push-staff", password="staff-password", is_staff=True
        )
        inactive_staff = staff_model.objects.create_user(
            username="inactive-push-staff",
            password="staff-password",
            is_staff=True,
            is_active=False,
        )
        for user, endpoint in (
            (active_staff, "https://fcm.googleapis.com/fcm/send/active"),
            (inactive_staff, "https://fcm.googleapis.com/fcm/send/inactive"),
        ):
            StaffPushSubscription.objects.create(
                user=user,
                endpoint=endpoint,
                p256dh="p256dh-key",
                auth="auth-key",
            )
        order = Order.objects.create(
            guest_name="Guest",
            phone="+256700000000",
            order_type=Order.OrderType.DINE_IN,
            location="Table 1",
        )

        notify_new_order(order)

        send_push.assert_called_once()
        self.assertEqual(
            send_push.call_args.kwargs["subscription_info"]["endpoint"],
            "https://fcm.googleapis.com/fcm/send/active",
        )

    def test_guest_cart_uses_backend_token_and_catalog_prices(self):
        create_response = self.client.post("/api/v1/cart/create/", {}, format="json")
        token = create_response.data["token"]
        stored_cart = GuestCart.objects.get()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

        add_response = self.client.post(
            "/api/v1/cart/items/",
            {"product": self.product.slug, "quantity": 2},
            format="json",
        )
        cart_response = self.client.get("/api/v1/cart/")

        self.assertEqual(create_response.status_code, 201)
        self.assertNotEqual(stored_cart.token_digest, token)
        self.assertEqual(add_response.status_code, 200)
        self.assertEqual(cart_response.data["item_count"], 2)
        self.assertEqual(cart_response.data["subtotal_ugx"], 24000)
        self.assertEqual(cart_response.data["items"][0]["price"], 12000)

    def test_guest_cart_imports_existing_browser_items_on_creation(self):
        response = self.client.post(
            "/api/v1/cart/create/",
            {"items": [{"product": self.product.slug, "quantity": 3}]},
            format="json",
        )
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {response.data['token']}"
        )

        cart_response = self.client.get("/api/v1/cart/")

        self.assertEqual(response.status_code, 201)
        self.assertEqual(cart_response.data["item_count"], 3)
        self.assertEqual(cart_response.data["subtotal_ugx"], 36000)

    def test_guest_cart_rejects_missing_token(self):
        response = self.client.get("/api/v1/cart/")

        self.assertEqual(response.status_code, 401)

    def test_guest_cart_updates_and_removes_items(self):
        create_response = self.client.post("/api/v1/cart/create/", {}, format="json")
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {create_response.data['token']}"
        )
        self.client.post(
            "/api/v1/cart/items/",
            {"product": self.product.slug, "quantity": 2},
            format="json",
        )

        update_response = self.client.patch(
            f"/api/v1/cart/items/{self.product.slug}/",
            {"quantity": 4},
            format="json",
        )
        self.assertEqual(update_response.data["item_count"], 4)

        remove_response = self.client.delete(
            f"/api/v1/cart/items/{self.product.slug}/"
        )
        self.assertEqual(remove_response.status_code, 200)
        self.assertEqual(remove_response.data["item_count"], 0)

    def test_guest_cart_order_uses_server_items_and_clears_the_cart(self):
        create_response = self.client.post("/api/v1/cart/create/", {}, format="json")
        token = create_response.data["token"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        self.client.post(
            "/api/v1/cart/items/",
            {"product": self.product.slug, "quantity": 2},
            format="json",
        )

        order_response = self.client.post(
            "/api/v1/orders/",
            {
                "guest_name": "A Guest",
                "phone": "+256700000000",
                "order_type": "dine_in",
                "location": "Table 4",
                "payment_method": "cash",
                "cart_token": token,
            },
            format="json",
        )

        self.assertEqual(order_response.status_code, 201)
        order = Order.objects.get(number=order_response.data["number"])
        self.assertEqual(order.total_ugx, 24000)
        self.assertEqual(order.items.get().quantity, 2)
        self.assertEqual(self.client.get("/api/v1/cart/").data["item_count"], 0)

    def test_customer_can_track_order_with_number_and_email_without_private_fields(self):
        order_response = self.client.post(
            "/api/v1/orders/",
            {
                "guest_name": "A Guest",
                "phone": "+256700000000",
                "email": "guest@example.com",
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
                "email": "GUEST@example.com",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], Order.Status.NEW)
        self.assertEqual(response.data["status_label"], "New")
        self.assertEqual(response.data["items"][0]["product_name"], self.product.name)
        self.assertNotIn("email", response.data)
        self.assertNotIn("phone", response.data)
        self.assertNotIn("guest_name", response.data)

    def test_customer_order_tracking_requires_matching_email(self):
        order_response = self.client.post(
            "/api/v1/orders/",
            {
                "guest_name": "A Guest",
                "phone": "+256700000000",
                "email": "guest@example.com",
                "order_type": "dine_in",
                "location": "Table 4",
                "items": [{"product": self.product.slug, "quantity": 1}],
            },
            format="json",
        )

        response = self.client.post(
            "/api/v1/orders/track/",
            {"number": order_response.data["number"], "email": "another@example.com"},
            format="json",
        )

        self.assertEqual(response.status_code, 404)

    def test_customer_tracking_expires_after_two_days_but_order_remains_for_staff(self):
        order_response = self.client.post(
            "/api/v1/orders/",
            {
                "guest_name": "A Guest",
                "phone": "+256700000000",
                "email": "guest@example.com",
                "order_type": "dine_in",
                "location": "Table 4",
                "items": [{"product": self.product.slug, "quantity": 1}],
            },
            format="json",
        )
        order = Order.objects.get(number=order_response.data["number"])
        Order.objects.filter(pk=order.pk).update(
            created_at=timezone.now() - timedelta(days=2, minutes=1)
        )

        tracking_response = self.client.post(
            "/api/v1/orders/track/",
            {"number": order.number, "email": "guest@example.com"},
            format="json",
        )

        self.assertEqual(tracking_response.status_code, 410)
        self.assertTrue(Order.objects.filter(pk=order.pk).exists())

    def test_customer_tracking_reflects_staff_order_and_payment_updates(self):
        order_response = self.client.post(
            "/api/v1/orders/",
            {
                "guest_name": "A Guest",
                "phone": "+256700000000",
                "email": "guest@example.com",
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
            {"number": order_response.data["number"], "email": "guest@example.com"},
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
        self.assertEqual(staff_response.data[0]["email"], "")
        self.assertEqual(
            staff_response.data[0]["items"][0]["product_image_url"],
            self.product.image_url,
        )
        tracking_response = self.client.post(
            "/api/v1/orders/track/",
            {
                "number": order_response.data["number"],
                "email": "guest@example.com",
            },
            format="json",
        )
        self.assertEqual(tracking_response.status_code, 404)

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
        check_in = date.today() + timedelta(days=10)
        check_out = check_in + timedelta(days=2)
        response = self.client.post(
            "/api/v1/reservations/",
            {
                "lodge": lodge.slug,
                "guest_name": "A Guest",
                "email": "guest@example.com",
                "phone": "+256700000000",
                "check_in": check_in.isoformat(),
                "check_out": check_out.isoformat(),
                "adults": 2,
                "children": 1,
                "status": LodgeReservation.Status.CONFIRMED,
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
                "email": "another@example.com",
                "phone": "+256700000001",
                "check_in": check_in.isoformat(),
                "check_out": (check_in + timedelta(days=1)).isoformat(),
                "adults": 4,
                "children": 0,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    @patch("core.api_views.notify_staff_new_reservation")
    def test_pending_reservation_schedules_staff_push_after_commit(self, push_reservation):
        lodge = Lodge.objects.create(name="Push Cottage")
        check_in = date.today() + timedelta(days=12)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                "/api/v1/reservations/",
                {
                    "lodge": lodge.slug,
                    "guest_name": "A Guest",
                    "email": "guest@example.com",
                    "phone": "+256700000000",
                    "check_in": check_in.isoformat(),
                    "check_out": (check_in + timedelta(days=2)).isoformat(),
                    "adults": 2,
                    "children": 0,
                },
                format="json",
            )

        self.assertEqual(response.status_code, 201)
        push_reservation.assert_called_once()
        self.assertEqual(
            push_reservation.call_args.args[0].reference,
            response.data["reference"],
        )

    def test_pending_reservation_and_blockouts_are_excluded_from_availability(self):
        lodge = Lodge.objects.create(name="Cedar Cottage", capacity=4)
        today = date.today()
        LodgeReservation.objects.create(
            lodge=lodge,
            guest_name="Pending Guest",
            phone="+256700000001",
            check_in=today + timedelta(days=4),
            check_out=today + timedelta(days=7),
            status=LodgeReservation.Status.PENDING,
        )
        LodgeBlockout.objects.create(
            lodge=lodge,
            start_date=today + timedelta(days=8),
            end_date=today + timedelta(days=10),
            reason="Maintenance",
        )

        overlapping = self.client.get(
            "/api/v1/lodges/",
            {
                "check_in": (today + timedelta(days=6)).isoformat(),
                "check_out": (today + timedelta(days=8)).isoformat(),
                "guests": 2,
            },
        )
        blocked = self.client.get(
            "/api/v1/lodges/",
            {
                "check_in": (today + timedelta(days=9)).isoformat(),
                "check_out": (today + timedelta(days=11)).isoformat(),
            },
        )
        duplicate = self.client.post(
            "/api/v1/reservations/",
            {
                "lodge": lodge.slug,
                "guest_name": "Second Guest",
                "email": "second@example.com",
                "phone": "+256700000002",
                "check_in": (today + timedelta(days=6)).isoformat(),
                "check_out": (today + timedelta(days=8)).isoformat(),
                "adults": 2,
                "children": 0,
            },
            format="json",
        )
        self.assertEqual(overlapping.status_code, 200)
        self.assertEqual(blocked.status_code, 200)
        self.assertEqual(overlapping.data, [])
        self.assertEqual(blocked.data, [])
        self.assertEqual(duplicate.status_code, 400)
        self.assertIn("lodge", duplicate.data)

    def test_lodge_calendar_marks_booked_and_blocked_checkin_dates_unavailable(self):
        lodge = Lodge.objects.create(name="Calendar Cottage", capacity=4)
        month_start = (date.today().replace(day=1) + timedelta(days=32)).replace(day=1)
        booked_check_in = month_start + timedelta(days=10)
        LodgeReservation.objects.create(
            lodge=lodge,
            guest_name="Booked Guest",
            phone="+256700000001",
            check_in=booked_check_in,
            check_out=booked_check_in + timedelta(days=2),
            status=LodgeReservation.Status.PENDING,
        )
        blocked_start = month_start + timedelta(days=15)
        LodgeBlockout.objects.create(
            lodge=lodge,
            start_date=blocked_start,
            end_date=blocked_start + timedelta(days=2),
            reason="Maintenance",
        )

        response = self.client.get(
            f"/api/v1/lodges/{lodge.slug}/availability/",
            {
                "month": month_start.strftime("%Y-%m"),
                "nights": 2,
                "guests": 2,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(booked_check_in.isoformat(), response.data["unavailable_dates"])
        self.assertIn((booked_check_in - timedelta(days=1)).isoformat(), response.data["unavailable_dates"])
        self.assertIn((blocked_start - timedelta(days=1)).isoformat(), response.data["unavailable_dates"])
        self.assertIn((booked_check_in + timedelta(days=2)).isoformat(), response.data["available_dates"])

    def test_public_reservation_enforces_lodge_stay_rules(self):
        lodge = Lodge.objects.create(name="Minimum Stay Lodge", min_nights=2)
        response = self.client.post(
            "/api/v1/reservations/",
            {
                "lodge": lodge.slug,
                "guest_name": "A Guest",
                "email": "guest@example.com",
                "phone": "+256700000000",
                "check_in": (date.today() + timedelta(days=5)).isoformat(),
                "check_out": (date.today() + timedelta(days=6)).isoformat(),
                "adults": 1,
                "children": 0,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("check_out", response.data)

    def test_reservation_submission_emails_guest_and_staff_when_configured(self):
        lodge = Lodge.objects.create(name="Email Cottage")
        check_in = date.today() + timedelta(days=9)
        with (
            override_settings(
                EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
                EMAIL_HOST="smtp.example.com",
                DEFAULT_FROM_EMAIL="reservations@example.com",
                RESEND_API_KEY="",
                STAFF_NOTIFICATION_EMAIL="",
                RESERVATION_NOTIFICATION_EMAIL="frontdesk@example.com",
            ),
            self.captureOnCommitCallbacks(execute=True),
        ):
            response = self.client.post(
                "/api/v1/reservations/",
                {
                    "lodge": lodge.slug,
                    "guest_name": "Email Guest",
                    "email": "guest@example.com",
                    "phone": "+256700000000",
                    "check_in": check_in.isoformat(),
                    "check_out": (check_in + timedelta(days=2)).isoformat(),
                    "adults": 1,
                    "children": 0,
                },
                format="json",
            )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(mail.outbox[0].to, ["guest@example.com"])
        self.assertEqual(mail.outbox[1].to, ["frontdesk@example.com"])


@override_settings(
    ADMIN_PORTAL_URL="https://admin.kalitusi.com",
    DEFAULT_FROM_EMAIL="notifications@kalitusi.com",
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    EMAIL_HOST="smtp.resend.com",
    EMAIL_HOST_PASSWORD="test-resend-key",
    EMAIL_HOST_USER="resend",
    EMAIL_PORT=587,
    EMAIL_USE_TLS=True,
    RESEND_API_KEY="",
    STAFF_NOTIFICATION_EMAIL="admin@example.com",
)
class TransactionalEmailAndPasswordResetTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.staff = get_user_model().objects.create_user(
            username="email-staff",
            password="initial-password",
            email="admin@example.com",
            is_staff=True,
        )
        self.staff_token = Token.objects.create(user=self.staff)
        self.category = Category.objects.create(name="Email test category")
        self.product = Product.objects.create(
            name="Email test item",
            category=self.category,
            price_ugx=12000,
        )

    def test_order_creation_emails_customer_thank_you_and_staff_alert(self):
        self.product.image_url = (
            "https://res.cloudinary.com/kalitusi/image/upload/sample-product.jpg"
        )
        self.product.save(update_fields=["image_url"])
        with (
            self.captureOnCommitCallbacks(execute=True),
            patch("core.api_views.notify_new_order"),
        ):
            response = self.client.post(
                "/api/v1/orders/",
                {
                    "guest_name": "Order Guest",
                    "phone": "+256700000000",
                    "email": "guest@example.com",
                    "order_type": "dine_in",
                    "location": "Table 2",
                    "items": [{"product": self.product.slug, "quantity": 2}],
                },
                format="json",
            )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(mail.outbox[0].to, ["guest@example.com"])
        self.assertIn("Thank you", mail.outbox[0].subject)
        self.assertIn("Thank You for Your Order!", mail.outbox[0].body)
        self.assertIn("Hello Order Guest,", mail.outbox[0].body)
        self.assertIn("Order Number | " + response.data["number"], mail.outbox[0].body)
        self.assertIn("Order Status | New", mail.outbox[0].body)
        self.assertIn("Payment Status | Unpaid", mail.outbox[0].body)
        self.assertIn("Order Type | Dine-in", mail.outbox[0].body)
        self.assertIn("Table | Table 2", mail.outbox[0].body)
        self.assertIn("Item | Quantity | Unit Price | Subtotal", mail.outbox[0].body)
        self.assertIn("Total Amount: UGX 24,000", mail.outbox[0].body)
        self.assertIn("Experience Nature. Experience Kalitusi.", mail.outbox[0].body)
        self.assertIn(response.data["number"], mail.outbox[0].body)
        order_html = mail.outbox[0].alternatives[0].content
        self.assertIn("Kalitusi%20Nature%20Resort", order_html)
        self.assertIn(self.product.image_url, order_html)
        self.assertIn("<table", order_html)
        self.assertEqual(mail.outbox[1].to, ["admin@example.com"])
        self.assertIn("New order", mail.outbox[1].subject)

    def test_order_status_and_payment_changes_email_customer_and_staff(self):
        order = Order.objects.create(
            guest_name="Order Guest",
            phone="+256700000000",
            email="guest@example.com",
            order_type=Order.OrderType.DINE_IN,
            location="Table 2",
        )
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Token {self.staff_token.key}"
        )
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.patch(
                f"/api/v1/staff/orders/{order.pk}/",
                {
                    "status": Order.Status.ACCEPTED,
                    "payment_status": Order.PaymentStatus.PAID,
                },
                format="json",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(mail.outbox[0].to, ["guest@example.com"])
        self.assertIn("Your Order Has Been Confirmed!", mail.outbox[0].body)
        self.assertIn("begin preparing it shortly", mail.outbox[0].body)
        self.assertIn("What's Next?", mail.outbox[0].body)
        self.assertIn("Confirmed", mail.outbox[0].body)
        self.assertIn("Paid", mail.outbox[0].body)
        self.assertEqual(mail.outbox[1].to, ["admin@example.com"])

    def test_order_status_transitions_have_distinct_customer_messages(self):
        order = Order.objects.create(
            guest_name="Order Guest",
            phone="+256700000000",
            email="guest@example.com",
            order_type=Order.OrderType.DINE_IN,
            location="Table 56",
        )
        transitions = (
            (
                Order.Status.ACCEPTED,
                "Your Order Has Been Confirmed!",
                "Confirmed",
            ),
            (
                Order.Status.PREPARING,
                "Your Order Is Being Prepared!",
                "Preparing",
            ),
            (Order.Status.READY, "Your Order Is Ready!", "Ready"),
            (
                Order.Status.COMPLETED,
                "Your Order Has Been Completed!",
                "Completed",
            ),
        )
        previous_status = Order.Status.NEW

        for status_value, heading, status_label in transitions:
            with self.subTest(status=status_value):
                order.status = status_value
                notify_order_updated(
                    order,
                    previous_status,
                    Order.PaymentStatus.UNPAID,
                )
                customer_email = mail.outbox[0]
                self.assertIn(heading, customer_email.subject)
                self.assertIn(heading, customer_email.body)
                self.assertIn(f"Order Status | {status_label}", customer_email.body)
                self.assertIn("What's Next?", customer_email.body)
                previous_status = status_value
                mail.outbox.clear()

    def test_reservation_creation_and_confirmation_email_customer_and_staff(self):
        lodge = Lodge.objects.create(
            name="Email Cottage",
            image_url="https://res.cloudinary.com/kalitusi/image/upload/email-cottage.jpg",
        )
        check_in = date.today() + timedelta(days=30)
        with (
            self.captureOnCommitCallbacks(execute=True),
            patch("core.api_views.notify_staff_new_reservation"),
        ):
            response = self.client.post(
                "/api/v1/reservations/",
                {
                    "lodge": lodge.slug,
                    "guest_name": "Reservation Guest",
                    "email": "guest@example.com",
                    "phone": "+256700000000",
                    "check_in": check_in.isoformat(),
                    "check_out": (check_in + timedelta(days=2)).isoformat(),
                },
                format="json",
            )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(len(mail.outbox), 2)
        self.assertIn("received", mail.outbox[0].subject)
        reservation_html = mail.outbox[0].alternatives[0].content
        self.assertIn(lodge.image_url, reservation_html)
        self.assertIn("<table", reservation_html)
        self.assertEqual(mail.outbox[1].to, ["admin@example.com"])

        mail.outbox.clear()
        reservation = LodgeReservation.objects.get(
            reference=response.data["reference"]
        )
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Token {self.staff_token.key}"
        )
        with self.captureOnCommitCallbacks(execute=True):
            confirmed = self.client.patch(
                f"/api/v1/staff/reservations/{reservation.pk}/",
                {"status": LodgeReservation.Status.CONFIRMED},
                format="json",
            )
        self.assertEqual(confirmed.status_code, 200)
        self.assertEqual(len(mail.outbox), 2)
        self.assertIn("Confirmed", mail.outbox[0].subject)
        self.assertEqual(mail.outbox[1].to, ["admin@example.com"])

    def test_staff_account_creation_alerts_operations_without_sending_password(self):
        owner = get_user_model().objects.create_superuser(
            username="email-owner",
            email="owner@example.com",
            password="OwnerPassphrase!934",
        )
        owner_token = Token.objects.create(user=owner)
        owner_client = APIClient()
        owner_client.credentials(HTTP_AUTHORIZATION=f"Token {owner_token.key}")

        with self.captureOnCommitCallbacks(execute=True):
            response = owner_client.post(
                "/api/v1/staff/accounts/",
                {
                    "username": "email-new-staff",
                    "email": "new-staff@example.com",
                    "first_name": "New",
                    "last_name": "Staff",
                },
                format="json",
            )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["admin@example.com"])
        self.assertIn("New staff account", mail.outbox[0].subject)
        self.assertNotIn(response.data["temporary_password"], mail.outbox[0].body)

    def test_staff_password_reset_is_generic_single_use_and_updates_credentials(self):
        account_profile = StaffProfile.objects.create(
            user=self.staff,
            must_change_password=True,
        )
        missing = self.client.post(
            "/api/v1/auth/password-reset/",
            {"email": "unknown@example.com"},
            format="json",
        )
        self.assertEqual(missing.status_code, 200)
        self.assertEqual(len(mail.outbox), 0)

        requested = self.client.post(
            "/api/v1/auth/password-reset/",
            {"email": self.staff.email},
            format="json",
        )
        self.assertEqual(requested.status_code, 200)
        self.assertEqual(requested.data, missing.data)
        self.assertEqual(len(mail.outbox), 1)
        reset_url = next(
            line
            for line in mail.outbox[0].body.splitlines()
            if line.startswith("https://admin.kalitusi.com/")
        )
        query = parse_qs(urlsplit(reset_url).query)
        payload = {
            "uid": query["uid"][0],
            "token": query["token"][0],
            "new_password": "NewSecureStaffPassphrase!527",
        }

        with self.captureOnCommitCallbacks(execute=True):
            confirmed = self.client.post(
                "/api/v1/auth/password-reset/confirm/",
                payload,
                format="json",
            )
        self.assertEqual(confirmed.status_code, 200)
        self.staff.refresh_from_db()
        account_profile.refresh_from_db()
        self.assertTrue(self.staff.check_password(payload["new_password"]))
        self.assertFalse(account_profile.must_change_password)
        self.assertFalse(Token.objects.filter(user=self.staff).exists())
        self.assertEqual(len(mail.outbox), 2)

        replayed = self.client.post(
            "/api/v1/auth/password-reset/confirm/",
            payload,
            format="json",
        )
        self.assertEqual(replayed.status_code, 400)

    @override_settings(EMAIL_HOST="", DEFAULT_FROM_EMAIL="")
    def test_staff_password_reset_logs_which_email_settings_are_empty(self):
        with self.assertLogs("core.email_notifications", level="WARNING") as logs:
            response = self.client.post(
                "/api/v1/auth/password-reset/",
                {"email": self.staff.email},
                format="json",
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("EMAIL_HOST, DEFAULT_FROM_EMAIL", logs.output[0])
        self.assertNotIn(self.staff.email, logs.output[0])
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(RESEND_API_KEY="test-resend-key")
    def test_staff_password_reset_uses_resend_https_api(self):
        response = MagicMock()
        response.status = 200
        response.__enter__.return_value = response
        response.read.return_value = b""
        with patch("core.email_notifications.urlopen", return_value=response) as urlopen:
            send_staff_password_reset_email(self.staff)

        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.resend.com/emails")
        self.assertEqual(
            request.get_header("Authorization"),
            "Bearer test-resend-key",
        )
        self.assertEqual(request.get_header("User-agent"), "kalitusi-backend/1.0")
        payload = json.loads(request.data)
        self.assertEqual(payload["to"], [self.staff.email])
        self.assertEqual(payload["from"], "notifications@kalitusi.com")
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(RESEND_API_KEY="test-resend-key")
    def test_resend_rejection_logs_provider_reason(self):
        error_body = json.dumps(
            {
                "name": "validation_error",
                "message": "The kalitusi.com domain is not verified.",
            }
        ).encode("utf-8")
        error = HTTPError(
            "https://api.resend.com/emails",
            403,
            "Forbidden",
            {},
            BytesIO(error_body),
        )

        with (
            self.assertLogs("core.email_notifications", level="ERROR") as logs,
            patch("core.email_notifications.urlopen", side_effect=error),
        ):
            send_staff_password_reset_email(self.staff)

        self.assertIn("HTTP 403", logs.output[0])
        self.assertIn("validation_error", logs.output[0])
        self.assertIn("domain is not verified", logs.output[0])

    @override_settings(RESEND_API_KEY="test-resend-key")
    def test_resend_rejection_logs_redacted_plain_text_response(self):
        error = HTTPError(
            "https://api.resend.com/emails",
            403,
            "Forbidden",
            {},
            BytesIO(b"Forbidden for user@example.com"),
        )

        with (
            self.assertLogs("core.email_notifications", level="ERROR") as logs,
            patch("core.email_notifications.urlopen", side_effect=error),
        ):
            send_staff_password_reset_email(self.staff)

        self.assertIn("Forbidden for [email redacted]", logs.output[0])
        self.assertNotIn("user@example.com", logs.output[0])

    @override_settings(RESEND_API_KEY="test-resend-key")
    def test_order_confirmation_includes_images_and_tables_in_resend_html(self):
        self.product.image_url = (
            "https://res.cloudinary.com/kalitusi/image/upload/order-item.jpg"
        )
        self.product.save(update_fields=["image_url"])
        order = Order.objects.create(
            guest_name="Order Guest",
            phone="+256700000000",
            email="guest@example.com",
            order_type=Order.OrderType.DINE_IN,
            location="Table 2",
            total_ugx=24000,
        )
        OrderItem.objects.create(
            order=order,
            product=self.product,
            product_name=self.product.name,
            unit_price_ugx=self.product.price_ugx,
            quantity=2,
            line_total_ugx=24000,
        )
        response = MagicMock()
        response.status = 200
        response.__enter__.return_value = response
        response.read.return_value = b""

        with patch("core.email_notifications.urlopen", return_value=response) as urlopen:
            notify_order_created(order)

        request = urlopen.call_args_list[0].args[0]
        payload = json.loads(request.data)
        self.assertIn(self.product.image_url, payload["html"])
        self.assertIn("Kalitusi%20Nature%20Resort", payload["html"])
        self.assertIn("<table", payload["html"])
        self.assertEqual(urlopen.call_count, 2)


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
        cache.clear()
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

    def test_staff_can_register_and_remove_a_push_subscription(self):
        self.assertEqual(
            self.client.get("/api/v1/staff/push/vapid-public-key/").status_code,
            401,
        )
        self.login_as_staff()
        with override_settings(
            VAPID_PUBLIC_KEY="public-key",
            VAPID_PRIVATE_KEY="private-key",
            VAPID_SUBJECT="mailto:admin@kalitusi.com",
        ):
            public_key = self.client.get("/api/v1/staff/push/vapid-public-key/")
            self.assertEqual(public_key.status_code, 200)
            self.assertEqual(public_key.data["public_key"], "public-key")

            payload = {
                "endpoint": "https://fcm.googleapis.com/fcm/send/test-endpoint",
                "keys": {"p256dh": "public-subscription-key", "auth": "auth-secret"},
            }
            response = self.client.post(
                "/api/v1/staff/push/subscriptions/", payload, format="json"
            )
            self.assertEqual(response.status_code, 201)
            self.assertTrue(
                StaffPushSubscription.objects.filter(user=self.staff).exists()
            )
            denied = self.client.post(
                "/api/v1/staff/push/subscriptions/",
                {
                    **payload,
                    "endpoint": "https://example.com/not-a-push-service",
                },
                format="json",
            )
            self.assertEqual(denied.status_code, 400)

            deleted = self.client.delete(
                "/api/v1/staff/push/subscriptions/",
                {"endpoint": payload["endpoint"]},
                format="json",
            )
            self.assertEqual(deleted.status_code, 204)
            self.assertFalse(
                StaffPushSubscription.objects.filter(user=self.staff).exists()
            )

    def test_staff_cancellation_reasons_are_saved_and_returned(self):
        self.login_as_staff()
        order = Order.objects.create(
            guest_name="Cancellation Guest",
            phone="+256700000009",
            order_type=Order.OrderType.DINE_IN,
            location="Table 4",
        )
        missing_order_reason = self.client.patch(
            f"/api/v1/staff/orders/{order.id}/",
            {"status": Order.Status.CANCELLED},
            format="json",
        )
        self.assertEqual(missing_order_reason.status_code, 400)
        self.assertIn("cancellation_reason", missing_order_reason.data)
        order_response = self.client.patch(
            f"/api/v1/staff/orders/{order.id}/",
            {
                "status": Order.Status.CANCELLED,
                "cancellation_reason": "Guest changed their plans.",
            },
            format="json",
        )
        self.assertEqual(order_response.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.cancellation_reason, "Guest changed their plans.")

        order_listing = self.client.get("/api/v1/staff/orders/")
        self.assertEqual(order_listing.status_code, 200)
        self.assertEqual(
            order_listing.data[0]["cancellation_reason"],
            "Guest changed their plans.",
        )

        check_in = date.today() + timedelta(days=30)
        reservation = LodgeReservation.objects.create(
            lodge=self.lodge,
            guest_name="Cancellation Guest",
            phone="+256700000009",
            check_in=check_in,
            check_out=check_in + timedelta(days=2),
            status=LodgeReservation.Status.CONFIRMED,
        )
        missing_reservation_reason = self.client.patch(
            f"/api/v1/staff/reservations/{reservation.id}/",
            {"status": LodgeReservation.Status.CANCELLED},
            format="json",
        )
        self.assertEqual(missing_reservation_reason.status_code, 400)
        self.assertIn("cancellation_reason", missing_reservation_reason.data)
        reservation_response = self.client.patch(
            f"/api/v1/staff/reservations/{reservation.id}/",
            {
                "status": LodgeReservation.Status.CANCELLED,
                "cancellation_reason": "Guest requested a cancellation.",
            },
            format="json",
        )
        self.assertEqual(reservation_response.status_code, 200)
        reservation.refresh_from_db()
        self.assertEqual(
            reservation.cancellation_reason,
            "Guest requested a cancellation.",
        )
        self.assertEqual(
            reservation_response.data["cancellation_reason"],
            "Guest requested a cancellation.",
        )

    def test_staff_can_update_profile_and_change_password(self):
        self.login_as_staff()
        old_token = Token.objects.get(user=self.staff).key

        response = self.client.get("/api/v1/staff/account/")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["must_change_password"])

        response = self.client.patch(
            "/api/v1/staff/account/",
            {"first_name": "Resort", "last_name": "Manager", "email": "manager@example.com"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["first_name"], "Resort")
        self.assertEqual(response.data["email"], "manager@example.com")

        response = self.client.post(
            "/api/v1/staff/account/password/verify/",
            {"current_password": "wrong-password"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        response = self.client.post(
            "/api/v1/staff/account/password/verify/",
            {"current_password": "staff-password"},
            format="json",
        )
        self.assertEqual(response.status_code, 204)

        response = self.client.post(
            "/api/v1/staff/account/password/",
            {
                "current_password": "staff-password",
                "new_password": "SecureStaffPassphrase!47",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response.data["token"], old_token)
        self.staff.refresh_from_db()
        self.assertTrue(self.staff.check_password("SecureStaffPassphrase!47"))
        self.assertNotIn(old_token, Token.objects.values_list("key", flat=True))

        self.client.credentials(HTTP_AUTHORIZATION=f"Token {response.data['token']}")
        self.assertEqual(self.client.get("/api/v1/staff/dashboard/").status_code, 200)
        self.assertEqual(self.client.get("/api/v1/staff/accounts/").status_code, 403)

    def test_superuser_can_create_staff_with_forced_temporary_password(self):
        superuser = get_user_model().objects.create_superuser(
            username="portal-owner",
            email="owner@example.com",
            password="owner-password",
        )
        owner_client = APIClient()
        login = owner_client.post(
            "/api/v1/auth/token/",
            {"username": "portal-owner", "password": "owner-password"},
            format="json",
        )
        self.assertEqual(login.status_code, 200)
        owner_client.credentials(HTTP_AUTHORIZATION=f"Token {login.data['token']}")

        response = owner_client.post(
            "/api/v1/staff/accounts/",
            {
                "username": "new-server",
                "email": "server@example.com",
                "first_name": "Dining",
                "last_name": "Staff",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        staff_data = response.data["staff"]
        temporary_password = response.data["temporary_password"]
        self.assertEqual(staff_data["username"], "new-server")
        self.assertTrue(staff_data["must_change_password"])
        self.assertFalse(staff_data["is_superuser"])
        self.assertTrue(get_user_model().objects.get(username="new-server").is_staff)
        self.assertTrue(superuser.is_superuser)

        new_staff_client = APIClient()
        login = new_staff_client.post(
            "/api/v1/auth/token/",
            {"username": "new-server", "password": temporary_password},
            format="json",
        )
        self.assertEqual(login.status_code, 200)
        self.assertTrue(login.data["must_change_password"])
        new_staff_client.credentials(HTTP_AUTHORIZATION=f"Token {login.data['token']}")
        self.assertEqual(
            new_staff_client.get("/api/v1/staff/dashboard/").status_code,
            403,
        )
        self.assertEqual(new_staff_client.get("/api/v1/staff/account/").status_code, 200)

        changed = new_staff_client.post(
            "/api/v1/staff/account/password/",
            {
                "current_password": temporary_password,
                "new_password": "NewServerPassphrase!29",
            },
            format="json",
        )
        self.assertEqual(changed.status_code, 200)
        profile = StaffProfile.objects.get(user__username="new-server")
        self.assertFalse(profile.must_change_password)
        new_staff_client.credentials(HTTP_AUTHORIZATION=f"Token {changed.data['token']}")
        self.assertEqual(
            new_staff_client.get("/api/v1/staff/dashboard/").status_code,
            200,
        )

    def test_superuser_can_reset_password_and_deactivate_staff(self):
        owner = get_user_model().objects.create_superuser(
            username="account-owner",
            email="owner@example.com",
            password="owner-password",
        )
        owner_client = APIClient()
        login = owner_client.post(
            "/api/v1/auth/token/",
            {"username": owner.username, "password": "owner-password"},
            format="json",
        )
        owner_client.credentials(HTTP_AUTHORIZATION=f"Token {login.data['token']}")
        staff = get_user_model().objects.create_user(
            username="team-member",
            password="initial-password",
            is_staff=True,
        )
        staff_token = Token.objects.create(user=staff)
        staff_client = APIClient()
        staff_client.credentials(HTTP_AUTHORIZATION=f"Token {staff_token.key}")

        response = owner_client.post(
            f"/api/v1/staff/accounts/{staff.pk}/reset-password/",
            {},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["staff"]["must_change_password"])
        self.assertFalse(Token.objects.filter(user=staff).exists())
        self.assertEqual(staff_client.get("/api/v1/staff/dashboard/").status_code, 401)

        response = owner_client.patch(
            f"/api/v1/staff/accounts/{staff.pk}/",
            {"is_active": False},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        staff.refresh_from_db()
        self.assertFalse(staff.is_active)

        response = owner_client.patch(
            f"/api/v1/staff/accounts/{owner.pk}/",
            {"is_active": False},
            format="json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertTrue(get_user_model().objects.filter(pk=staff.pk).exists())

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
        response = self.client.get("/api/v1/staff/reservations/calendar/")
        self.assertEqual(response.status_code, 401)
        response = self.client.post("/api/v1/staff/lodge-blockouts/", {}, format="json")
        self.assertEqual(response.status_code, 401)

        self.client.force_authenticate(user=self.regular_user)
        response = self.client.get("/api/v1/staff/products/")
        self.assertEqual(response.status_code, 403)
        response = self.client.get("/api/v1/staff/reservations/")
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
        self.assertTrue(
            response.data["image_url"].startswith("https://res.cloudinary.com/")
        )
        upload_image.assert_called_once()
        public_id = upload_image.call_args.kwargs["public_id"]
        self.assertTrue(public_id.startswith("kalitusi/products/"))
        asset_uuid = public_id.rsplit("/", 1)[-1]
        self.assertEqual(str(uuid.UUID(asset_uuid)), asset_uuid)
        self.assertFalse(upload_image.call_args.kwargs["overwrite"])

        public_products = self.client.get("/api/v1/products/")
        uploaded_product = next(
            product
            for product in public_products.data
            if product["id"] == response.data["id"]
        )
        self.assertEqual(uploaded_product["image"], response.data["image_url"])

    def test_staff_can_upload_lodge_image_for_public_cottages(self):
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
                        f"https://res.cloudinary.com/demo/{options['public_id']}.webp"
                    ),
                    "public_id": options["public_id"],
                },
            ) as upload_image,
        ):
            response = self.client.patch(
                f"/api/v1/staff/lodges/{self.lodge.pk}/",
                {
                    "image_file": SimpleUploadedFile(
                        "cottage.webp", b"sample image", content_type="image/webp"
                    )
                },
                format="multipart",
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            response.data["image_url"].startswith("https://res.cloudinary.com/")
        )
        upload_image.assert_called_once()
        public_id = upload_image.call_args.kwargs["public_id"]
        self.assertTrue(public_id.startswith("kalitusi/lodges/"))
        asset_uuid = public_id.rsplit("/", 1)[-1]
        self.assertEqual(str(uuid.UUID(asset_uuid)), asset_uuid)
        self.assertFalse(upload_image.call_args.kwargs["overwrite"])

        public_lodges = self.client.get("/api/v1/lodges/")
        self.assertEqual(public_lodges.status_code, 200)
        self.assertEqual(public_lodges.data[0]["image_url"], response.data["image_url"])

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

    def test_staff_can_edit_reservation_details(self):
        self.login_as_staff()
        reservation = LodgeReservation.objects.create(
            lodge=self.lodge,
            guest_name="Original Guest",
            phone="+256700000001",
            check_in=date.today() + timedelta(days=8),
            check_out=date.today() + timedelta(days=10),
        )
        second_lodge = Lodge.objects.create(name="Garden Cottage", capacity=6)
        check_in = date.today() + timedelta(days=12)
        check_out = date.today() + timedelta(days=15)

        response = self.client.patch(
            f"/api/v1/staff/reservations/{reservation.id}/",
            {
                "lodge": second_lodge.id,
                "guest_name": "Updated Guest",
                "email": "guest@example.com",
                "phone": "+256700000099",
                "check_in": check_in.isoformat(),
                "check_out": check_out.isoformat(),
                "adults": 3,
                "children": 2,
                "note": "Ground-floor room requested",
                "status": LodgeReservation.Status.CONFIRMED,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["lodge"], second_lodge.id)
        self.assertEqual(response.data["lodge_name"], second_lodge.name)
        self.assertEqual(response.data["guest_name"], "Updated Guest")
        self.assertEqual(response.data["email"], "guest@example.com")
        self.assertEqual(response.data["phone"], "+256700000099")
        self.assertEqual(response.data["check_in"], check_in.isoformat())
        self.assertEqual(response.data["check_out"], check_out.isoformat())
        self.assertEqual(response.data["adults"], 3)
        self.assertEqual(response.data["children"], 2)
        self.assertEqual(response.data["note"], "Ground-floor room requested")
        self.assertEqual(response.data["status"], LodgeReservation.Status.CONFIRMED)

    def test_staff_reservation_edits_validate_dates_and_lodge_capacity(self):
        self.login_as_staff()
        reservation = LodgeReservation.objects.create(
            lodge=self.lodge,
            guest_name="Guest",
            phone="+256700000001",
            check_in=date.today() + timedelta(days=8),
            check_out=date.today() + timedelta(days=10),
        )
        url = f"/api/v1/staff/reservations/{reservation.id}/"

        response = self.client.patch(
            url,
            {"adults": 4, "children": 1},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("adults", response.data)

        response = self.client.patch(
            url,
            {
                "check_in": (date.today() + timedelta(days=10)).isoformat(),
                "check_out": (date.today() + timedelta(days=9)).isoformat(),
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("check_out", response.data)

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

    def test_staff_can_create_filter_calendar_and_delete_reservations(self):
        self.login_as_staff()
        check_in = date.today() + timedelta(days=14)
        check_out = check_in + timedelta(days=2)
        response = self.client.post(
            "/api/v1/staff/reservations/",
            {
                "lodge": self.lodge.id,
                "guest_name": "Walk-in Guest",
                "email": "walkin@example.com",
                "phone": "+256700000008",
                "check_in": check_in.isoformat(),
                "check_out": check_out.isoformat(),
                "adults": 2,
                "children": 0,
                "occasion": "Anniversary",
                "source": LodgeReservation.Source.WALK_IN,
                "status": LodgeReservation.Status.PENDING,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        reservation_id = response.data["id"]
        self.assertEqual(response.data["source"], LodgeReservation.Source.WALK_IN)
        self.assertEqual(response.data["occasion"], "Anniversary")

        listing = self.client.get(
            "/api/v1/staff/reservations/",
            {"q": "Walk-in", "source": LodgeReservation.Source.WALK_IN},
        )
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.data["count"], 1)
        self.assertEqual(listing.data["results"][0]["id"], reservation_id)

        calendar = self.client.get(
            "/api/v1/staff/reservations/calendar/",
            {"start": check_in.isoformat(), "end": check_out.isoformat()},
        )
        self.assertEqual(calendar.status_code, 200)
        self.assertEqual(calendar.data[0]["id"], reservation_id)

        response = self.client.patch(
            f"/api/v1/staff/reservations/{reservation_id}/",
            {
                "status": LodgeReservation.Status.CANCELLED,
                "cancellation_reason": "Guest asked to cancel.",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        response = self.client.delete(f"/api/v1/staff/reservations/{reservation_id}/")
        self.assertEqual(response.status_code, 204)

    def test_staff_can_manage_blockouts_and_blockouts_reject_active_stays(self):
        self.login_as_staff()
        start = date.today() + timedelta(days=20)
        end = start + timedelta(days=2)
        response = self.client.post(
            "/api/v1/staff/lodge-blockouts/",
            {
                "lodge": self.lodge.id,
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "reason": "Maintenance",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        blockout_id = response.data["id"]
        unavailable = self.client.get(
            "/api/v1/lodges/",
            {
                "check_in": start.isoformat(),
                "check_out": end.isoformat(),
                "guests": 2,
            },
        )
        self.assertEqual(unavailable.status_code, 200)
        self.assertEqual(unavailable.data, [])

        active_check_in = date.today() + timedelta(days=30)
        active_reservation = LodgeReservation.objects.create(
            lodge=self.lodge,
            guest_name="Active Guest",
            phone="+256700000001",
            check_in=active_check_in,
            check_out=active_check_in + timedelta(days=2),
            status=LodgeReservation.Status.CONFIRMED,
        )
        response = self.client.post(
            "/api/v1/staff/lodge-blockouts/",
            {
                "lodge": self.lodge.id,
                "start_date": active_reservation.check_in.isoformat(),
                "end_date": active_reservation.check_out.isoformat(),
                "reason": "Closed",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.client.delete(f"/api/v1/staff/lodge-blockouts/{blockout_id}/")
