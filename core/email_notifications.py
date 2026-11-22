import logging
import smtplib
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from .models import LodgeReservation, Order

logger = logging.getLogger(__name__)


def _send_email(subject: str, message: str, recipients: list[str], reference: str) -> None:
    if not recipients:
        return
    missing_settings = [
        name
        for name, value in (
            ("EMAIL_HOST", settings.EMAIL_HOST),
            ("DEFAULT_FROM_EMAIL", settings.DEFAULT_FROM_EMAIL),
        )
        if not value
    ]
    if missing_settings:
        logger.warning(
            "Email not sent for %s: required settings are empty: %s.",
            reference,
            ", ".join(missing_settings),
        )
        return
    try:
        sent = send_mail(
            subject,
            message,
            settings.DEFAULT_FROM_EMAIL,
            recipients,
            fail_silently=False,
        )
    except (OSError, smtplib.SMTPException) as error:
        logger.error(
            "Email delivery failed for %s (%s).",
            reference,
            type(error).__name__,
        )
        return
    if sent != len(recipients):
        logger.error("Email delivery was incomplete for %s.", reference)
    else:
        logger.info(
            "SMTP accepted email for %s (%d recipient(s)).",
            reference,
            sent,
        )


def _staff_recipients(reference: str) -> list[str]:
    email = settings.STAFF_NOTIFICATION_EMAIL or settings.RESERVATION_NOTIFICATION_EMAIL
    if not email:
        logger.warning(
            "Staff email alert not sent for %s: STAFF_NOTIFICATION_EMAIL is unset.",
            reference,
        )
        return []
    return [email]


def notify_order_created(order: Order) -> None:
    items = "\n".join(
        f"{item.quantity} x {item.product_name} - UGX {item.line_total_ugx:,}"
        for item in order.items.all()
    )
    details = (
        f"Order: {order.number}\n"
        f"Status: {order.get_status_display()}\n"
        f"Items:\n{items}\n"
        f"Total: UGX {order.total_ugx:,}"
    )
    if order.email:
        _send_email(
            f"Thank you for your order: {order.number}",
            f"Hello {order.guest_name},\n\n"
            "Thank you for ordering from Kalitusi Resort. We have received your "
            "order and will email you when its status changes.\n\n"
            f"{details}",
            [order.email],
            order.number,
        )
    _send_email(
        f"New order: {order.number}",
        (
            f"A new order has been received.\n\n{details}\n"
            f"Guest: {order.guest_name}\n"
            f"Phone: {order.phone}\n"
            f"Location: {order.location}"
        ),
        _staff_recipients(order.number),
        order.number,
    )


def notify_order_updated(
    order: Order, previous_status: str, previous_payment_status: str
) -> None:
    changes = []
    if order.status != previous_status:
        changes.append(
            f"Order status: {Order.Status(previous_status).label} -> "
            f"{order.get_status_display()}"
        )
    if order.payment_status != previous_payment_status:
        changes.append(
            f"Payment status: {Order.PaymentStatus(previous_payment_status).label} "
            f"-> {order.get_payment_status_display()}"
        )
    if not changes:
        return

    details = (
        f"Order: {order.number}\n"
        f"Status: {order.get_status_display()}\n"
        f"Payment: {order.get_payment_status_display()}\n"
        f"Total: UGX {order.total_ugx:,}"
    )
    change_summary = "\n".join(changes)
    if order.email:
        _send_email(
            f"Order update: {order.number}",
            f"Hello {order.guest_name},\n\n"
            f"There is an update to your order:\n{change_summary}\n\n{details}",
            [order.email],
            order.number,
        )
    _send_email(
        f"Order activity: {order.number}",
        (
            f"An order was updated.\n\n{change_summary}\n\n{details}\n"
            f"Guest: {order.guest_name}\n"
            f"Phone: {order.phone}"
        ),
        _staff_recipients(order.number),
        order.number,
    )


def notify_new_reservation(reservation: LodgeReservation) -> None:
    is_pending = reservation.status == LodgeReservation.Status.PENDING
    details = (
        f"Reference: {reservation.reference}\n"
        f"Guest: {reservation.guest_name}\n"
        f"Stay: {reservation.lodge.name}, "
        f"{reservation.check_in} to {reservation.check_out}\n"
        f"Guests: {reservation.adults} adults, {reservation.children} children\n"
        f"Status: {reservation.get_status_display()}"
    )
    if reservation.email:
        customer_message = (
            "Thank you for your reservation request. "
            "It has been received and is pending review."
            if is_pending
            else (
                "Thank you for your reservation. "
                f"Its current status is {reservation.get_status_display()}."
            )
        )
        _send_email(
            f"Reservation request received: {reservation.reference}"
            if is_pending
            else f"Reservation created: {reservation.reference}",
            f"Hello {reservation.guest_name},\n\n{customer_message}\n\n{details}",
            [reservation.email],
            reservation.reference,
        )
    _send_email(
        f"New reservation: {reservation.reference}",
        (
            "A new reservation request is awaiting review."
            if is_pending
            else f"A new reservation has status {reservation.get_status_display()}."
        )
        + f"\n\n{details}",
        _staff_recipients(reservation.reference),
        reservation.reference,
    )


def notify_reservation_status(reservation: LodgeReservation) -> None:
    message = (
        f"Hello {reservation.guest_name},\n\n"
        f"Your reservation status is now {reservation.get_status_display()}.\n\n"
        f"Stay: {reservation.lodge.name}, "
        f"{reservation.check_in} to {reservation.check_out}\n"
        f"Reference: {reservation.reference}"
    )
    if reservation.email:
        _send_email(
            f"Reservation {reservation.reference}: {reservation.get_status_display()}",
            message,
            [reservation.email],
            reservation.reference,
        )
    _send_email(
        f"Reservation activity: {reservation.reference}",
        (
            f"Reservation status changed to {reservation.get_status_display()}.\n\n"
            f"Reference: {reservation.reference}\n"
            f"Guest: {reservation.guest_name}\n"
            f"Stay: {reservation.lodge.name}, "
            f"{reservation.check_in} to {reservation.check_out}"
        ),
        _staff_recipients(reservation.reference),
        reservation.reference,
    )


def notify_staff_account_created(username: str, created_by: str) -> None:
    reference = f"staff account {username}"
    _send_email(
        f"New staff account: {username}",
        f"A staff account for {username} was created by {created_by}.",
        _staff_recipients(reference),
        reference,
    )


def send_staff_password_reset_email(user) -> None:
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    query = urlencode({"reset": "1", "uid": uid, "token": token})
    reset_url = f"{settings.ADMIN_PORTAL_URL}/?{query}"
    _send_email(
        "Reset your Kalitusi admin password",
        (
            f"Hello {user.get_username()},\n\n"
            "A password reset was requested for your Kalitusi staff account. "
            "Use the link below to choose a new password:\n\n"
            f"{reset_url}\n\n"
            "If you did not request this, you can ignore this email."
        ),
        [user.email],
        f"password reset for staff account {user.pk}",
    )


def notify_staff_password_reset_complete(user) -> None:
    _send_email(
        "Your Kalitusi admin password was changed",
        (
            f"Hello {user.get_username()},\n\n"
            "Your Kalitusi staff account password was reset. If you did not "
            "make this change, contact the resort administrator."
        ),
        [user.email],
        f"password reset for staff account {user.pk}",
    )
