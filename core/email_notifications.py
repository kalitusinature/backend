import json
import logging
import re
import smtplib
from html import escape
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import EmailMultiAlternatives, send_mail
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from .models import LodgeReservation, Order

logger = logging.getLogger(__name__)

KALITUSI_LOGO_URL = (
    "https://kalitusi-nature-resort.odoo.com/web/image/website/1/logo/"
    "Kalitusi%20Nature%20Resort?unique=7d8d223"
)


def _resend_error_detail(error: HTTPError) -> str:
    raw_body = error.read()
    if not raw_body:
        return "no response body"

    try:
        body = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        detail = raw_body.decode("utf-8", errors="replace")
    else:
        if isinstance(body, dict):
            details = [
                value
                for value in (body.get("name"), body.get("message"))
                if isinstance(value, str) and value
            ]
            detail = ": ".join(details) or json.dumps(body)
        else:
            detail = json.dumps(body)
    detail = re.sub(r"<[^>]*>", " ", detail)
    detail = re.sub(r"[\r\n\t]+", " ", detail)
    detail = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[email redacted]", detail)
    detail = re.sub(r"\bre_[A-Za-z0-9_-]+\b", "[API key redacted]", detail)
    detail = re.sub(r"(?i)\bbearer\s+\S+", "Bearer [redacted]", detail)
    return detail[:300]


def _send_with_resend_api(
    subject: str,
    message: str,
    recipients: list[str],
    reference: str,
    html_message: str | None = None,
) -> None:
    payload = json.dumps(
        {
            "from": settings.DEFAULT_FROM_EMAIL,
            "to": recipients,
            "subject": subject,
            "text": message,
            **({"html": html_message} if html_message else {}),
        }
    ).encode("utf-8")
    request = Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={
            "Authorization": f"Bearer {settings.RESEND_API_KEY}",
            "Content-Type": "application/json",
            "User-Agent": "kalitusi-backend/1.0",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=settings.EMAIL_TIMEOUT) as response:
            if not 200 <= response.status < 300:
                logger.error(
                    "Resend API rejected email for %s (HTTP %s).",
                    reference,
                    response.status,
                )
                return
            response.read()
    except HTTPError as error:
        detail = _resend_error_detail(error)
        logger.error(
            "Resend API rejected email for %s (HTTP %s%s).",
            reference,
            error.code,
            f", {detail}" if detail else "",
        )
        return
    except (OSError, URLError) as error:
        reason = getattr(error, "reason", None)
        error_type = type(reason).__name__ if reason is not None else type(error).__name__
        logger.error(
            "Resend API request failed for %s (%s).",
            reference,
            error_type,
        )
        return
    logger.info(
        "Resend API accepted email for %s (%d recipient(s)).",
        reference,
        len(recipients),
    )


def _send_email(
    subject: str,
    message: str,
    recipients: list[str],
    reference: str,
    html_message: str | None = None,
) -> None:
    if not recipients:
        return
    if settings.RESEND_API_KEY:
        if not settings.DEFAULT_FROM_EMAIL:
            logger.warning(
                "Email not sent for %s: required setting is empty: DEFAULT_FROM_EMAIL.",
                reference,
            )
            return
        _send_with_resend_api(subject, message, recipients, reference, html_message)
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
        if html_message:
            email = EmailMultiAlternatives(
                subject,
                message,
                settings.DEFAULT_FROM_EMAIL,
                recipients,
            )
            email.attach_alternative(html_message, "text/html")
            sent = email.send(fail_silently=False)
        else:
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
        logger.info("SMTP accepted email for %s (%d recipient(s)).", reference, sent)


def _staff_recipients(reference: str) -> list[str]:
    email = settings.STAFF_NOTIFICATION_EMAIL or settings.RESERVATION_NOTIFICATION_EMAIL
    if not email:
        logger.warning(
            "Staff email alert not sent for %s: STAFF_NOTIFICATION_EMAIL is unset.",
            reference,
        )
        return []
    return [email]


def _image_html(url: str, alt: str, width: int, height: int) -> str:
    parsed_url = urlsplit(url)
    if parsed_url.scheme.lower() != "https" or not parsed_url.netloc:
        return ""
    return (
        f'<img src="{escape(url, quote=True)}" alt="{escape(alt, quote=True)}" '
        f'width="{width}" height="{height}" '
        'style="display:block;max-width:100%;height:auto;border:0;'
        'border-radius:8px;margin:0 auto 8px;">'
    )


def _email_layout(subject: str, intro: str, content_html: str) -> str:
    logo_html = _image_html(
        KALITUSI_LOGO_URL,
        "Kalitusi Nature Resort",
        180,
        64,
    )
    return (
        '<!doctype html><html lang="en"><body '
        'style="margin:0;padding:24px;background:#f4f7f5;'
        'font-family:Arial,Helvetica,sans-serif;color:#24332d;">'
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
        'style="border-collapse:collapse;"><tr><td align="center">'
        '<table role="presentation" width="600" cellspacing="0" cellpadding="0" '
        'style="width:100%;max-width:600px;background:#ffffff;border-radius:12px;'
        'border-collapse:separate;padding:24px;">'
        f'<tr><td align="center" style="padding:8px 8px 24px;">{logo_html}</td></tr>'
        f'<tr><td><h1 style="margin:0 0 12px;font-size:24px;color:#234b3b;">'
        f'{escape(subject)}</h1><p style="margin:0 0 20px;line-height:1.6;">'
        f'{escape(intro)}</p>{content_html}</td></tr>'
        '<tr><td style="padding-top:24px;color:#64736c;font-size:13px;">'
        'Thank you,<br>Kalitusi Nature Resort</td></tr>'
        '</table></td></tr></table></body></html>'
    )


def _details_table_html(rows: list[tuple[str, str]]) -> str:
    table_rows = "".join(
        '<tr>'
        '<th scope="row" align="left" style="padding:10px;border-bottom:1px solid #e6ece8;'
        'color:#52645b;font-weight:600;">'
        f"{escape(label)}</th>"
        '<td align="left" style="padding:10px;border-bottom:1px solid #e6ece8;">'
        f"{escape(value)}</td></tr>"
        for label, value in rows
    )
    return (
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
        'style="border-collapse:collapse;margin:0 0 20px;">'
        f"<tbody>{table_rows}</tbody></table>"
    )


def _order_email_html(order: Order) -> str:
    item_rows = []
    for item in order.items.select_related("product").all():
        image_url = item.product.image_url if item.product else ""
        image = _image_html(image_url, item.product_name, 56, 56)
        item_rows.append(
            '<tr>'
            '<td style="padding:10px;border-bottom:1px solid #e6ece8;">'
            f'{image}<span>{escape(item.product_name)}</span></td>'
            f'<td align="center" style="padding:10px;border-bottom:1px solid #e6ece8;">'
            f"{item.quantity}</td>"
            '<td align="right" style="padding:10px;border-bottom:1px solid #e6ece8;">'
            f"UGX {item.unit_price_ugx:,}</td>"
            '<td align="right" style="padding:10px;border-bottom:1px solid #e6ece8;">'
            f"UGX {item.line_total_ugx:,}</td></tr>"
        )
    items_table = (
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
        'style="border-collapse:collapse;margin:0 0 20px;">'
        '<thead><tr style="background:#edf4ef;">'
        '<th align="left" style="padding:10px;">Item</th>'
        '<th align="center" style="padding:10px;">Qty</th>'
        '<th align="right" style="padding:10px;">Unit price</th>'
        '<th align="right" style="padding:10px;">Subtotal</th>'
        f"</tr></thead><tbody>{''.join(item_rows)}</tbody>"
        '<tfoot><tr><th colspan="3" align="right" style="padding:12px 10px;">'
        'Total</th><th align="right" style="padding:12px 10px;">'
        f"UGX {order.total_ugx:,}</th></tr></tfoot></table>"
    )
    details = _details_table_html(
        [
            ("Order", order.number),
            ("Status", order.get_status_display()),
            ("Payment", order.get_payment_status_display()),
            ("Order type", order.get_order_type_display()),
            ("Location", order.location),
        ]
    )
    return details + items_table


def _reservation_email_html(reservation: LodgeReservation) -> str:
    lodge_image = _image_html(
        reservation.lodge.image_url,
        f"{reservation.lodge.name} accommodation",
        552,
        240,
    )
    details = _details_table_html(
        [
            ("Reservation", reservation.reference),
            ("Lodge", reservation.lodge.name),
            ("Check-in", reservation.check_in.isoformat()),
            ("Check-out", reservation.check_out.isoformat()),
            ("Adults", str(reservation.adults)),
            ("Children", str(reservation.children)),
            ("Status", reservation.get_status_display()),
        ]
    )
    return lodge_image + details


def notify_order_created(order: Order) -> None:
    items = "\n".join(
        f"{item.quantity} x {item.product_name} - UGX {item.line_total_ugx:,}"
        for item in order.items.all()
    )
    order_html = _order_email_html(order)
    customer_intro = (
        "Thank you for ordering from Kalitusi Resort. We have received your "
        "order and will email you when its status changes."
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
            f"Hello {order.guest_name},\n\n{customer_intro}\n\n{details}",
            [order.email],
            order.number,
            html_message=_email_layout(
                f"Thank you for your order: {order.number}",
                f"Hello {order.guest_name}, {customer_intro}",
                order_html,
            ),
        )
    staff_intro = "A new order has been received."
    _send_email(
        f"New order: {order.number}",
        (
            f"{staff_intro}\n\n{details}\n"
            f"Guest: {order.guest_name}\n"
            f"Phone: {order.phone}\n"
            f"Location: {order.location}"
        ),
        _staff_recipients(order.number),
        order.number,
        html_message=_email_layout(
            f"New order: {order.number}",
            staff_intro,
            order_html,
        ),
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
    order_html = _order_email_html(order)
    customer_intro = f"There is an update to your order: {'; '.join(changes)}"
    if order.email:
        _send_email(
            f"Order update: {order.number}",
            f"Hello {order.guest_name},\n\n{customer_intro}\n\n{details}",
            [order.email],
            order.number,
            html_message=_email_layout(
                f"Order update: {order.number}",
                f"Hello {order.guest_name}, {customer_intro}",
                order_html,
            ),
        )
    staff_intro = f"An order was updated: {'; '.join(changes)}"
    _send_email(
        f"Order activity: {order.number}",
        (
            f"{staff_intro}\n\n{details}\n"
            f"Guest: {order.guest_name}\n"
            f"Phone: {order.phone}"
        ),
        _staff_recipients(order.number),
        order.number,
        html_message=_email_layout(
            f"Order activity: {order.number}",
            staff_intro,
            order_html,
        ),
    )


def notify_new_reservation(reservation: LodgeReservation) -> None:
    is_pending = reservation.status == LodgeReservation.Status.PENDING
    reservation_html = _reservation_email_html(reservation)
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
            html_message=_email_layout(
                f"Reservation {reservation.get_status_display()}",
                f"Hello {reservation.guest_name}, {customer_message}",
                reservation_html,
            ),
        )
    staff_intro = (
        "A new reservation request is awaiting review."
        if is_pending
        else f"A new reservation has status {reservation.get_status_display()}."
    )
    _send_email(
        f"New reservation: {reservation.reference}",
        f"{staff_intro}\n\n{details}",
        _staff_recipients(reservation.reference),
        reservation.reference,
        html_message=_email_layout(
            f"New reservation: {reservation.reference}",
            staff_intro,
            reservation_html,
        ),
    )


def notify_reservation_status(reservation: LodgeReservation) -> None:
    reservation_html = _reservation_email_html(reservation)
    customer_intro = (
        f"Your reservation status is now {reservation.get_status_display()}."
    )
    message = (
        f"Hello {reservation.guest_name},\n\n"
        f"{customer_intro}\n\n"
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
            html_message=_email_layout(
                f"Reservation {reservation.reference}: "
                f"{reservation.get_status_display()}",
                f"Hello {reservation.guest_name}, {customer_intro}",
                reservation_html,
            ),
        )
    staff_intro = f"Reservation status changed to {reservation.get_status_display()}."
    _send_email(
        f"Reservation activity: {reservation.reference}",
        f"{staff_intro}\n\n"
        f"Reference: {reservation.reference}\n"
        f"Guest: {reservation.guest_name}\n"
        f"Stay: {reservation.lodge.name}, "
        f"{reservation.check_in} to {reservation.check_out}",
        _staff_recipients(reservation.reference),
        reservation.reference,
        html_message=_email_layout(
            f"Reservation activity: {reservation.reference}",
            staff_intro,
            reservation_html,
        ),
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
