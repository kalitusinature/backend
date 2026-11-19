import logging
import smtplib

from django.conf import settings
from django.core.mail import send_mail

from .models import LodgeReservation

logger = logging.getLogger(__name__)


def _send(subject: str, message: str, recipients: list[str], reference: str) -> None:
    if not recipients:
        return
    if not settings.EMAIL_HOST or not settings.DEFAULT_FROM_EMAIL:
        logger.warning(
            "Reservation email not sent for %s: SMTP settings are incomplete.",
            reference,
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
            "Reservation email delivery failed for %s (%s).",
            reference,
            type(error).__name__,
        )
        return
    if sent != len(recipients):
        logger.error(
            "Reservation email delivery was incomplete for %s.",
            reference,
        )


def notify_new_reservation(reservation) -> None:
    is_pending = reservation.status == LodgeReservation.Status.PENDING
    details = (
        f"Reference: {reservation.reference}\n"
        f"Guest: {reservation.guest_name}\n"
        f"Stay: {reservation.lodge.name}, {reservation.check_in} to {reservation.check_out}\n"
        f"Guests: {reservation.adults} adults, {reservation.children} children\n"
        f"Status: {reservation.get_status_display()}"
    )
    if reservation.email:
        customer_message = (
            "Your request has been received and is pending review."
            if is_pending
            else f"Your reservation has been created with status {reservation.get_status_display()}."
        )
        _send(
            f"Reservation request received: {reservation.reference}"
            if is_pending
            else f"Reservation created: {reservation.reference}",
            f"{customer_message}\n\n{details}",
            [reservation.email],
            reservation.reference,
        )
    admin_email = settings.RESERVATION_NOTIFICATION_EMAIL
    if admin_email:
        _send(
            f"New reservation request: {reservation.reference}"
            if is_pending
            else f"New reservation created: {reservation.reference}",
            (
                "A reservation request is awaiting review."
                if is_pending
                else f"A reservation was created with status {reservation.get_status_display()}."
            )
            + f"\n\n{details}",
            [admin_email],
            reservation.reference,
        )
    else:
        logger.warning(
            "Reservation alert not sent for %s: RESERVATION_NOTIFICATION_EMAIL is unset.",
            reservation.reference,
        )


def notify_reservation_status(reservation) -> None:
    if not reservation.email:
        return
    _send(
        f"Reservation {reservation.reference}: {reservation.get_status_display()}",
        (
            f"Your reservation status is now {reservation.get_status_display()}.\n\n"
            f"Stay: {reservation.lodge.name}, "
            f"{reservation.check_in} to {reservation.check_out}\n"
            f"Reference: {reservation.reference}"
        ),
        [reservation.email],
        reservation.reference,
    )
