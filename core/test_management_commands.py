from datetime import date, timedelta
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.db import connection
from django.test import TestCase
from django.utils import timezone
from rest_framework.authtoken.models import Token

from .models import (
    Category,
    ChatConversation,
    ChatEmailVerification,
    ChatMessage,
    CustomerReview,
    GuestCart,
    GuestCartItem,
    Lodge,
    LodgeBlockout,
    LodgeReservation,
    Order,
    OrderEmailVerification,
    OrderItem,
    Payment,
    PaymentReceipt,
    Product,
    StaffProfile,
    StaffPushSubscription,
    SupportTicket,
)


class ClearTransactionalDataCommandTests(TestCase):
    def setUp(self):
        self.staff = get_user_model().objects.create_user(
            username="cleanup-admin",
            password="test-password",
            is_staff=True,
        )
        self.token = Token.objects.create(user=self.staff)
        StaffProfile.objects.create(user=self.staff)
        StaffPushSubscription.objects.create(
            user=self.staff,
            endpoint="https://push.example.test/subscription",
            p256dh="test-key",
            auth="test-auth",
        )
        self.category = Category.objects.create(name="Cleanup test category")
        self.product = Product.objects.create(
            name="Cleanup test product",
            category=self.category,
            price_ugx=1000,
        )
        self.lodge = Lodge.objects.create(name="Cleanup test lodge")
        self.blockout = LodgeBlockout.objects.create(
            lodge=self.lodge,
            start_date=date(2030, 1, 1),
            end_date=date(2030, 1, 2),
            reason="Preserve availability setup",
        )

        cart = GuestCart.objects.create(token_digest="a" * 64)
        GuestCartItem.objects.create(cart=cart, product=self.product, quantity=1)
        order = Order.objects.create(
            guest_name="Test Guest",
            phone="+256700000000",
            order_type=Order.OrderType.DINE_IN,
            location="Table 1",
        )
        OrderItem.objects.create(
            order=order,
            product=self.product,
            product_name=self.product.name,
            unit_price_ugx=1000,
            quantity=1,
            line_total_ugx=1000,
        )
        reservation = LodgeReservation.objects.create(
            lodge=self.lodge,
            guest_name="Test Guest",
            phone="+256700000000",
            check_in=date(2030, 1, 1),
            check_out=date(2030, 1, 2),
        )
        order_payment = Payment.objects.create(
            order=order,
            amount_ugx=1000,
            method=Payment.Method.CASH,
            transaction_reference="cleanup-order-payment",
            verified_by=self.staff,
        )
        reservation_payment = Payment.objects.create(
            reservation=reservation,
            amount_ugx=1000,
            method=Payment.Method.CASH,
            transaction_reference="cleanup-reservation-payment",
            verified_by=self.staff,
        )
        PaymentReceipt.objects.create(
            payment=order_payment,
            number="CLEANUP-ORDER-RECEIPT",
            verification_token_digest="b" * 64,
            total_due_ugx=1000,
            paid_to_date_ugx=1000,
            balance_ugx=0,
            pdf_data=b"test-pdf",
        )
        PaymentReceipt.objects.create(
            payment=reservation_payment,
            number="CLEANUP-STAY-RECEIPT",
            verification_token_digest="c" * 64,
            total_due_ugx=1000,
            paid_to_date_ugx=1000,
            balance_ugx=0,
            pdf_data=b"test-pdf",
        )
        CustomerReview.objects.create(
            product=self.product,
            order=order,
            reviewer_name="Test Guest",
            rating=5,
            comment="Test review",
        )
        CustomerReview.objects.create(
            lodge=self.lodge,
            reservation=reservation,
            reviewer_name="Test Guest",
            rating=5,
            comment="Test stay review",
        )
        OrderEmailVerification.objects.create(
            email="test@example.com",
            code_hash="hash",
            expires_at=timezone.now() + timedelta(days=1),
        )
        conversation = ChatConversation.objects.create(
            email="chat@example.com",
            guest_name="Test Guest",
        )
        ChatMessage.objects.create(
            conversation=conversation,
            sender=ChatMessage.Sender.CUSTOMER,
            body="Test message",
        )
        ChatEmailVerification.objects.create(
            name="Test Guest",
            email="chat@example.com",
            code_hash="hash",
            expires_at=timezone.now() + timedelta(days=1),
        )
        SupportTicket.objects.create(
            name="Test Guest",
            email="test@example.com",
            subject="Test ticket",
            message="Test message",
        )

    def _confirm_options(self):
        options = {
            "confirm": True,
            "expected_database": str(connection.settings_dict["NAME"]),
            "stdout": StringIO(),
        }
        host = connection.settings_dict.get("HOST") or ""
        if host:
            options["expected_host"] = host
        return options

    def test_dry_run_reports_records_without_deleting_them(self):
        output = StringIO()

        call_command("clear_transactional_data", stdout=output)

        self.assertIn("Dry run only", output.getvalue())
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(Payment.objects.count(), 2)
        self.assertEqual(PaymentReceipt.objects.count(), 2)
        self.assertEqual(CustomerReview.objects.count(), 2)

    def test_confirm_requires_exact_database_name(self):
        options = self._confirm_options()
        options["expected_database"] = "not-the-test-database"

        with self.assertRaisesMessage(CommandError, "must exactly match"):
            call_command("clear_transactional_data", **options)

        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(LodgeReservation.objects.count(), 1)

    def test_confirm_clears_activity_and_preserves_staff_and_setup(self):
        call_command("clear_transactional_data", **self._confirm_options())

        for model in (
            CustomerReview,
            PaymentReceipt,
            Payment,
            OrderItem,
            Order,
            LodgeReservation,
            GuestCartItem,
            GuestCart,
            OrderEmailVerification,
            ChatMessage,
            ChatConversation,
            ChatEmailVerification,
            SupportTicket,
        ):
            with self.subTest(model=model.__name__):
                self.assertEqual(model.objects.count(), 0)

        self.assertTrue(get_user_model().objects.filter(pk=self.staff.pk).exists())
        self.assertTrue(Token.objects.filter(pk=self.token.pk).exists())
        self.assertTrue(StaffProfile.objects.filter(user=self.staff).exists())
        self.assertTrue(
            StaffPushSubscription.objects.filter(user=self.staff).exists()
        )
        self.assertTrue(Category.objects.filter(pk=self.category.pk).exists())
        self.assertTrue(Product.objects.filter(pk=self.product.pk).exists())
        self.assertTrue(Lodge.objects.filter(pk=self.lodge.pk).exists())
        self.assertTrue(LodgeBlockout.objects.filter(pk=self.blockout.pk).exists())
