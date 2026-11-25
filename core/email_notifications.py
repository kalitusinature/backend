import base64
import json
import logging
import re
import smtplib
from datetime import date
from html import escape
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import EmailMultiAlternatives, send_mail
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from .models import (
    ChatConversation,
    ChatMessage,
    LodgeReservation,
    Order,
    Payment,
    PaymentReceipt,
    SupportTicket,
)
from .review_tokens import (
    order_is_review_eligible,
    order_review_invitation_links,
    reservation_is_review_eligible,
    reservation_review_invitation_links,
)

logger = logging.getLogger(__name__)

KALITUSI_LOGO_URL = (
    "https://kalitusi-nature-resort.odoo.com/web/image/website/1/logo/"
    "Kalitusi%20Nature%20Resort?unique=7d8d223"
)
ORDER_STATUS_EMAIL_COPY: dict[str, tuple[str, str, str, str]] = {
    Order.Status.NEW: (
        "Your Order Has Been Received!",
        "We've received your order at Kalitusi Nature Resort.",
        "Our team will review it and update you when it is confirmed.",
        "No action is needed while our team reviews your order.",
    ),
    Order.Status.ACCEPTED: (
        "Your Order Has Been Confirmed!",
        "Good news! Your order at Kalitusi Nature Resort has been confirmed.",
        "Our team has received your order and will begin preparing it shortly. "
        "You can find your order details below.",
        "Your order is confirmed, and our team will take care of the rest. "
        "We'll keep you updated if there are any further changes to your order.",
    ),
    Order.Status.PREPARING: (
        "Your Order Is Being Prepared!",
        "Our team is now preparing your order at Kalitusi Nature Resort.",
        "We are carefully preparing your order and will notify you when it is ready.",
        "You can contact the resort if you need assistance while we prepare it.",
    ),
    Order.Status.READY: (
        "Your Order Is Ready!",
        "Your order at Kalitusi Nature Resort is ready.",
        "Our team has finished preparing your order.",
        "Please check in with our team to receive your order at the resort.",
    ),
    Order.Status.COMPLETED: (
        "Your Order Has Been Completed!",
        "Your order has been completed.",
        "We hope you enjoyed your experience at Kalitusi Nature Resort.",
        "Thank you for choosing Kalitusi. We look forward to serving you again.",
    ),
    Order.Status.CANCELLED: (
        "Your Order Has Been Cancelled",
        "Your order at Kalitusi Nature Resort has been cancelled.",
        "If you have questions about this change, please contact the resort team.",
        "Our team can help with any questions about your order.",
    ),
}
RESERVATION_STATUS_EMAIL_COPY: dict[str, dict[str, str]] = {
    LodgeReservation.Status.PENDING: {
        "heading": "Your Reservation Request Has Been Received!",
        "intro": "Thank you for choosing Kalitusi Nature Resort!",
        "detail": (
            "We're pleased to let you know that we have received your "
            "reservation request. Our team is currently reviewing the details, "
            "and your reservation is pending confirmation."
        ),
        "next_heading": "What Happens Next?",
        "next_step": (
            "Our reservations team will review your request and notify you by "
            "email once your reservation has been approved or if any "
            "additional information is required."
        ),
        "notice": (
            "Please note that your reservation is not yet confirmed. We "
            "recommend waiting for our confirmation before making travel "
            "arrangements."
        ),
        "closing": (
            "Thank you for considering Kalitusi Nature Resort. We look forward "
            "to welcoming you!"
        ),
    },
    LodgeReservation.Status.CONFIRMED: {
        "heading": "Your Reservation Is Confirmed!",
        "intro": (
            "Good news! Your reservation at Kalitusi Nature Resort is confirmed."
        ),
        "detail": "Your accommodation is booked.",
        "next_heading": "What Happens Next?",
        "next_step": (
            "We look forward to welcoming you. Please contact our reservations "
            "team if you have any questions before arrival."
        ),
        "notice": "",
        "closing": (
            "Thank you for choosing Kalitusi Nature Resort. We look forward to "
            "welcoming you!"
        ),
    },
    LodgeReservation.Status.DECLINED: {
        "heading": "Reservation Request Update",
        "intro": (
            "Unfortunately, we cannot accommodate your reservation request."
        ),
        "detail": "Your reservation request has been rejected.",
        "next_heading": "Need Help?",
        "next_step": (
            "Please contact our reservations team if you would like help with "
            "alternative dates or accommodation."
        ),
        "notice": "",
        "closing": "Thank you for considering Kalitusi Nature Resort.",
    },
    LodgeReservation.Status.CANCELLED: {
        "heading": "Your Reservation Has Been Cancelled",
        "intro": "Your reservation at Kalitusi Nature Resort has been cancelled.",
        "detail": "The latest reservation details are included below.",
        "next_heading": "Need Help?",
        "next_step": (
            "If you need help with a future booking, our reservations team is "
            "here to assist."
        ),
        "notice": "",
        "closing": "Thank you for considering Kalitusi Nature Resort.",
    },
    LodgeReservation.Status.CHECKED_IN: {
        "heading": "Welcome to Kalitusi!",
        "intro": "Welcome! Your stay at Kalitusi Nature Resort has begun.",
        "detail": "We hope you enjoy your time with us.",
        "next_heading": "During Your Stay",
        "next_step": (
            "If you need assistance during your stay, please contact our team."
        ),
        "notice": "",
        "closing": "Thank you for choosing Kalitusi Nature Resort.",
    },
    LodgeReservation.Status.COMPLETED: {
        "heading": "Thank You for Staying with Us!",
        "intro": "Thank you for staying at Kalitusi Nature Resort.",
        "detail": "We hope you had a wonderful stay.",
        "next_heading": "We Hope to Welcome You Back",
        "next_step": (
            "We would love to welcome you back again. Please contact us when "
            "you are ready to plan your next stay."
        ),
        "notice": "",
        "closing": "Thank you for choosing Kalitusi Nature Resort.",
    },
}


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
    attachments: list[tuple[str, bytes, str]] | None = None,
) -> bool:
    resend_attachments = [
        {
            "filename": filename,
            "content": base64.b64encode(content).decode("ascii"),
            "content_type": content_type,
        }
        for filename, content, content_type in attachments or []
    ]
    payload = json.dumps(
        {
            "from": settings.DEFAULT_FROM_EMAIL,
            "to": recipients,
            "subject": subject,
            "text": message,
            **({"html": html_message} if html_message else {}),
            **({"attachments": resend_attachments} if resend_attachments else {}),
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
                return False
            response.read()
    except HTTPError as error:
        detail = _resend_error_detail(error)
        logger.error(
            "Resend API rejected email for %s (HTTP %s%s).",
            reference,
            error.code,
            f", {detail}" if detail else "",
        )
        return False
    except (OSError, URLError) as error:
        reason = getattr(error, "reason", None)
        error_type = type(reason).__name__ if reason is not None else type(error).__name__
        logger.error(
            "Resend API request failed for %s (%s).",
            reference,
            error_type,
        )
        return False
    logger.info(
        "Resend API accepted email for %s (%d recipient(s)).",
        reference,
        len(recipients),
    )
    return True


def _send_email(
    subject: str,
    message: str,
    recipients: list[str],
    reference: str,
    html_message: str | None = None,
    attachments: list[tuple[str, bytes, str]] | None = None,
) -> bool:
    if not recipients:
        return False
    if settings.RESEND_API_KEY:
        if not settings.DEFAULT_FROM_EMAIL:
            logger.warning(
                "Email not sent for %s: required setting is empty: DEFAULT_FROM_EMAIL.",
                reference,
            )
            return False
        return _send_with_resend_api(
            subject,
            message,
            recipients,
            reference,
            html_message,
            attachments,
        )

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
        return False
    try:
        if html_message or attachments:
            email = EmailMultiAlternatives(
                subject,
                message,
                settings.DEFAULT_FROM_EMAIL,
                recipients,
            )
            if html_message:
                email.attach_alternative(html_message, "text/html")
            for filename, content, content_type in attachments or []:
                email.attach(filename, content, content_type)
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
        return False
    if sent != len(recipients):
        logger.error("Email delivery was incomplete for %s.", reference)
        return False
    else:
        logger.info("SMTP accepted email for %s (%d recipient(s)).", reference, sent)
        return True


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


def _email_layout(
    subject: str,
    intro: str,
    content_html: str,
    closing_html: str | None = None,
) -> str:
    logo_html = _image_html(
        KALITUSI_LOGO_URL,
        "Kalitusi Nature Resort",
        180,
        64,
    )
    closing = closing_html or (
        '<tr><td style="padding-top:24px;color:#64736c;font-size:13px;">'
        'Thank you,<br>Kalitusi Nature Resort</td></tr>'
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
        f"{closing}"
        '</table></td></tr></table></body></html>'
    )


def send_order_history_verification_code(email: str, code: str) -> bool:
    subject = "Your Kalitusi order history verification code"
    message = (
        f"Your verification code is {code}.\n\n"
        "It expires in 10 minutes and can only be used once. "
        "If you did not request this code, you can ignore this email."
    )
    html_message = _email_layout(
        "Verify your order history",
        "Use this one-time code to view orders placed with your email address.",
        (
            '<p style="padding:16px;border-radius:8px;background:#f4f7f5;'
            'color:#234b3b;font-size:28px;font-weight:700;letter-spacing:8px;'
            f'text-align:center;">{escape(code)}</p>'
            '<p style="line-height:1.6;">This code expires in 10 minutes and '
            'can only be used once. If you did not request it, ignore this email.</p>'
        ),
    )
    return _send_email(
        subject,
        message,
        [email],
        "order-history-verification",
        html_message=html_message,
    )


def send_chat_verification_code(email: str, code: str) -> bool:
    subject = "Your Kalitusi chat verification code"
    message = (
        f"Your chat verification code is {code}.\n\n"
        "It expires in 10 minutes and can only be used once. "
        "If you did not request it, you can ignore this email."
    )
    html_message = _email_layout(
        "Verify your email to chat with us",
        "Use this one-time code to open your Kalitusi Resort support chat.",
        (
            '<p style="padding:16px;border-radius:8px;background:#f4f7f5;'
            'color:#234b3b;font-size:28px;font-weight:700;letter-spacing:8px;'
            f'text-align:center;">{escape(code)}</p>'
            '<p style="line-height:1.6;">This code expires in 10 minutes and '
            'can only be used once. If you did not request it, ignore this email.</p>'
        ),
    )
    return _send_email(
        subject,
        message,
        [email],
        "customer-chat-verification",
        html_message=html_message,
    )


def send_chat_admin_notification(
    conversation: ChatConversation,
    message: ChatMessage,
) -> bool:
    subject = f"New customer chat from {conversation.guest_name}"
    message_text = (
        f"New message from {conversation.guest_name} ({conversation.email}):\n\n"
        f"{message.body}\n\n"
        f"Reply in the Kalitusi admin portal: {settings.ADMIN_PORTAL_URL}"
    )
    html_message = _email_layout(
        subject,
        f"New support message from {conversation.guest_name} ({conversation.email}).",
        (
            '<blockquote style="margin:0;padding:16px;border-left:3px solid #008b92;'
            'background:#f4f7f5;line-height:1.6;">'
            f"{escape(message.body)}</blockquote>"
        ),
        closing_html=(
            '<tr><td style="padding-top:24px;color:#64736c;font-size:13px;">'
            f'<a href="{escape(settings.ADMIN_PORTAL_URL, quote=True)}">'
            "Open the admin portal to reply.</a></td></tr>"
        ),
    )
    return _send_email(
        subject,
        message_text,
        _staff_recipients(f"customer-chat-{conversation.pk}"),
        f"customer-chat-{conversation.pk}",
        html_message=html_message,
    )


def send_support_ticket_notification(ticket: SupportTicket) -> bool:
    recipient = settings.SUPPORT_TICKET_EMAIL
    if not recipient:
        logger.warning(
            "Support ticket email not sent for ticket %s: SUPPORT_TICKET_EMAIL is unset.",
            ticket.pk,
        )
        return False

    subject = f"Support ticket #{ticket.pk}: {ticket.subject}"
    message = (
        f"Support ticket #{ticket.pk}\n\n"
        f"Name: {ticket.name}\n"
        f"Email: {ticket.email}\n"
        f"Phone: {ticket.phone or 'Not provided'}\n"
        f"Subject: {ticket.subject}\n\n"
        f"Message:\n{ticket.message}\n\n"
        f"View support tickets in the admin portal: {settings.ADMIN_PORTAL_URL}"
    )
    details_html = _details_table_html(
        [
            ("Ticket", f"#{ticket.pk}"),
            ("Name", ticket.name),
            ("Email", ticket.email),
            ("Phone", ticket.phone or "Not provided"),
            ("Subject", ticket.subject),
        ]
    )
    html_message = _email_layout(
        subject,
        f"New support ticket from {ticket.name}.",
        (
            f"{details_html}"
            '<blockquote style="margin:0;padding:16px;border-left:3px solid #008b92;'
            'background:#f4f7f5;line-height:1.6;white-space:pre-wrap;">'
            f"{escape(ticket.message)}</blockquote>"
        ),
        closing_html=(
            '<tr><td style="padding-top:24px;color:#64736c;font-size:13px;">'
            f'<a href="{escape(settings.ADMIN_PORTAL_URL, quote=True)}">'
            "Open the admin portal to review this ticket.</a></td></tr>"
        ),
    )
    return _send_email(
        subject,
        message,
        [recipient],
        f"support-ticket-{ticket.pk}",
        html_message=html_message,
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


def _review_rating_links_html(rating_urls: dict[int, str]) -> str:
    return "".join(
        '<a '
        f'href="{escape(rating_urls[rating], quote=True)}" '
        f'aria-label="Rate {rating} out of 5 stars" '
        'style="display:inline-block;width:44px;height:44px;line-height:44px;'
        'text-align:center;color:#a57516;font-size:30px;text-decoration:none;">'
        "★</a>"
        for rating in range(1, 6)
    )


def _send_review_invitation_email(
    *,
    subject: str,
    guest_name: str,
    email: str,
    reference: str,
    intro: str,
    review_items: list[dict],
) -> bool:
    if not email or not review_items:
        return False

    text_items = []
    html_items = []
    for item in review_items:
        name = item["name"]
        url = item["url"]
        rating_urls = item["rating_urls"]
        text_items.append(
            f"{name}\n"
            + "\n".join(
                f"{rating} star{'s' if rating != 1 else ''}: "
                f"{rating_urls[rating]}"
                for rating in range(1, 6)
            )
        )
        html_items.append(
            '<section style="margin:20px 0;padding:16px;border:1px solid #e6ece8;'
            'border-radius:8px;">'
            f'<h2 style="margin:0 0 8px;font-size:18px;color:#234b3b;">'
            f"{escape(name)}</h2>"
            '<p style="margin:0 0 8px;line-height:1.5;">Click a star to choose '
            "your rating:</p>"
            f'<div role="group" aria-label="Rate {escape(name, quote=True)}">'
            f"{_review_rating_links_html(rating_urls)}</div>"
            f'<p style="margin:8px 0 0;"><a href="{escape(url, quote=True)}" '
            'style="color:#007a80;">Open review page</a></p></section>'
        )

    instructions = (
        "Choose a star to open your secure review page with that rating selected. "
        "You can add a comment and submit your review there. No account sign-in "
        "is required; the private link verifies your completed purchase or stay."
    )
    text = (
        f"Hello {guest_name},\n\n{intro}\n\n{instructions}\n\n"
        + "\n\n".join(text_items)
        + "\n\nWarm regards,\nKalitusi Nature Resort\n"
        "Experience Nature. Experience Kalitusi."
    )
    html = (
        '<p style="line-height:1.6;">'
        f"{escape(intro)}</p>"
        f'<p style="line-height:1.6;">{escape(instructions)}</p>'
        f'{"".join(html_items)}'
    )
    return _send_email(
        subject,
        text,
        [email],
        reference,
        html_message=_email_layout(
            subject,
            f"Hello {guest_name},",
            html,
        ),
    )


def send_order_review_invitation_email(order: Order) -> bool | None:
    links = order_review_invitation_links(order)
    if not links:
        return None
    review_items = [
        {
            "name": link["product_name"],
            "url": link["url"],
            "rating_urls": link["rating_urls"],
        }
        for link in links
    ]
    return _send_review_invitation_email(
        subject=f"Rate your Kalitusi order - {order.number}",
        guest_name=order.guest_name,
        email=order.email,
        reference=f"order-review-{order.number}",
        intro=(
            f"Your completed and fully paid order {order.number} is eligible "
            "for a review."
        ),
        review_items=review_items,
    )


def send_reservation_review_invitation_email(
    reservation: LodgeReservation,
) -> bool | None:
    links = reservation_review_invitation_links(reservation)
    if not links:
        return None
    review_items = [
        {
            "name": reservation.lodge.name,
            "url": links["url"],
            "rating_urls": links["rating_urls"],
        }
    ]
    return _send_review_invitation_email(
        subject=f"Rate your Kalitusi stay - {reservation.reference}",
        guest_name=reservation.guest_name,
        email=reservation.email,
        reference=f"reservation-review-{reservation.reference}",
        intro=(
            f"Your checked-out and fully paid stay {reservation.reference} "
            "is eligible for a review."
        ),
        review_items=review_items,
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
        '<th align="center" style="padding:10px;">Quantity</th>'
        '<th align="right" style="padding:10px;">Unit Price</th>'
        '<th align="right" style="padding:10px;">Subtotal</th>'
        f"</tr></thead><tbody>{''.join(item_rows)}</tbody>"
        '<tfoot><tr><th colspan="3" align="right" style="padding:12px 10px;">'
        'Total Amount</th><th align="right" style="padding:12px 10px;">'
        f"UGX {order.total_ugx:,}</th></tr></tfoot></table>"
    )
    location_label = (
        "Table"
        if order.order_type == Order.OrderType.DINE_IN
        else "Lodge"
    )
    details = _details_table_html(
        [
            ("Order Number", order.number),
            ("Order Status", _order_status_label(order.status)),
            ("Payment Status", order.get_payment_status_display()),
            ("Order Type", order.get_order_type_display()),
            (location_label, order.location),
        ]
    )
    return (
        '<h2 style="margin:24px 0 12px;font-size:18px;color:#234b3b;">'
        'Order Summary</h2>'
        f"{details}"
        '<h2 style="margin:24px 0 12px;font-size:18px;color:#234b3b;">'
        'Order Details</h2>'
        f"{items_table}"
    )


def _order_summary_text(order: Order) -> str:
    location_label = (
        "Table"
        if order.order_type == Order.OrderType.DINE_IN
        else "Lodge"
    )
    lines = [
        "Order Summary",
        f"Order Number | {order.number}",
        f"Order Status | {_order_status_label(order.status)}",
        f"Payment Status | {order.get_payment_status_display()}",
        f"Order Type | {order.get_order_type_display()}",
        f"{location_label} | {order.location}",
        "",
        "Order Details",
        "Item | Quantity | Unit Price | Subtotal",
    ]
    for item in order.items.all():
        lines.append(
            f"{item.product_name} | {item.quantity} | "
            f"UGX {item.unit_price_ugx:,} | UGX {item.line_total_ugx:,}"
        )
    lines.append(f"Total Amount: UGX {order.total_ugx:,}")
    return "\n".join(lines)


def _order_status_label(status: str) -> str:
    if status == Order.Status.COMPLETED:
        return "Completed"
    return Order.Status(status).label


def _reservation_email_html(reservation: LodgeReservation) -> str:
    lodge_image = _image_html(
        reservation.lodge.image_url,
        f"{reservation.lodge.name} accommodation",
        552,
        240,
    )
    details = _details_table_html(
        [
            ("Reservation Number", reservation.reference),
            ("Accommodation", reservation.lodge.name),
            ("Check-in", _reservation_email_date(reservation.check_in)),
            ("Check-out", _reservation_email_date(reservation.check_out)),
            ("Adults", str(reservation.adults)),
            ("Children", str(reservation.children)),
            ("Reservation Status", reservation.get_status_display()),
        ]
    )
    return (
        lodge_image
        + '<h2 style="margin:24px 0 12px;font-size:18px;color:#234b3b;">'
        "Reservation Details</h2>"
        + details
    )


def _reservation_email_date(value: date) -> str:
    return f"{value.day} {value.strftime('%B %Y')}"


def _reservation_summary_text(reservation: LodgeReservation) -> str:
    return "\n".join(
        [
            "Reservation Details",
            f"Reservation Number | {reservation.reference}",
            f"Accommodation | {reservation.lodge.name}",
            f"Check-in | {_reservation_email_date(reservation.check_in)}",
            f"Check-out | {_reservation_email_date(reservation.check_out)}",
            f"Adults | {reservation.adults}",
            f"Children | {reservation.children}",
            f"Reservation Status | {reservation.get_status_display()}",
        ]
    )


def _reservation_customer_email(
    reservation: LodgeReservation,
) -> tuple[str, str, str]:
    copy = RESERVATION_STATUS_EMAIL_COPY[reservation.status]
    heading = copy["heading"]
    next_heading = copy["next_heading"]
    summary = _reservation_summary_text(reservation)
    signoff = (
        "Warm regards,\n\n"
        "Kalitusi Nature Resort\n"
        "Experience Nature. Experience Kalitusi."
    )
    notice_text = f"\n\n{copy['notice']}" if copy["notice"] else ""
    body = (
        f"{heading}\n\n"
        f"Hello {reservation.guest_name},\n\n"
        f"{copy['intro']}\n\n"
        f"{copy['detail']}\n\n"
        f"{summary}\n\n"
        f"{next_heading}\n"
        f"{copy['next_step']}"
        f"{notice_text}\n\n"
        f"{copy['closing']}\n\n"
        f"{signoff}"
    )
    notice_html = (
        f'<p style="line-height:1.6;">{escape(copy["notice"])}</p>'
        if copy["notice"]
        else ""
    )
    content_html = (
        f'<p style="line-height:1.6;">{escape(copy["intro"])}</p>'
        f'<p style="line-height:1.6;">{escape(copy["detail"])}</p>'
        f"{_reservation_email_html(reservation)}"
        '<h2 style="margin:24px 0 12px;font-size:18px;color:#234b3b;">'
        f"{escape(next_heading)}</h2>"
        f'<p style="line-height:1.6;">{escape(copy["next_step"])}</p>'
        f"{notice_html}"
    )
    closing_html = (
        '<tr><td style="padding-top:24px;color:#64736c;'
        'font-size:13px;line-height:1.7;">'
        f"{escape(copy['closing'])}<br><br>Warm regards,<br><br>"
        "<strong>Kalitusi Nature Resort</strong><br>"
        "<em>Experience Nature. Experience Kalitusi.</em>"
        "</td></tr>"
    )
    return (
        heading,
        body,
        _email_layout(
            heading,
            f"Hello {reservation.guest_name},",
            content_html,
            closing_html=closing_html,
        ),
    )


def notify_order_created(order: Order) -> None:
    items = "\n".join(
        f"{item.quantity} x {item.product_name} - UGX {item.line_total_ugx:,}"
        for item in order.items.all()
    )
    order_html = _order_email_html(order)
    customer_intro = (
        "Thank you for choosing Kalitusi Nature Resort!\n\n"
        "We're pleased to confirm that we have received your order. Our team "
        "will process it shortly, and you'll receive an email notification "
        "whenever your order status changes."
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
            (
                "Thank You for Your Order!\n\n"
                f"Hello {order.guest_name},\n\n"
                f"{customer_intro}\n\n"
                f"{_order_summary_text(order)}\n\n"
                "Thank you for choosing Kalitusi Nature Resort. We appreciate "
                "your business and look forward to serving you!\n\n"
                "Warm regards,\n\n"
                "Kalitusi Nature Resort\n"
                "Experience Nature. Experience Kalitusi."
            ),
            [order.email],
            order.number,
            html_message=_email_layout(
                "Thank You for Your Order!",
                f"Hello {order.guest_name},",
                (
                    '<p style="line-height:1.6;">Thank you for choosing '
                    '<strong>Kalitusi Nature Resort</strong>!</p>'
                    '<p style="line-height:1.6;">We\'re pleased to confirm '
                    "that we have received your order. Our team will process "
                    "it shortly, and you'll receive an email notification "
                    "whenever your order status changes.</p>"
                    f"{order_html}"
                ),
                closing_html=(
                    '<tr><td style="padding-top:24px;color:#64736c;'
                    'font-size:13px;line-height:1.7;">'
                    "Thank you for choosing <strong>Kalitusi Nature Resort"
                    "</strong>. We appreciate your business and look forward "
                    "to serving you!<br><br>Warm regards,<br><br>"
                    "<strong>Kalitusi Nature Resort</strong><br>"
                    "<em>Experience Nature. Experience Kalitusi.</em>"
                    "</td></tr>"
                ),
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
            f"Order status: {_order_status_label(previous_status)} -> "
            f"{_order_status_label(order.status)}"
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
        f"Status: {_order_status_label(order.status)}\n"
        f"Payment: {order.get_payment_status_display()}\n"
        f"Total: UGX {order.total_ugx:,}"
    )
    order_html = _order_email_html(order)
    if order.email:
        if order.status != previous_status:
            heading, intro, detail, next_step = ORDER_STATUS_EMAIL_COPY.get(
                order.status,
                (
                    "Your Order Has Been Updated",
                    "Your order at Kalitusi Nature Resort has been updated.",
                    "Please review the latest order information below.",
                    "Contact the resort team if you have questions.",
                ),
            )
        else:
            heading = "Your Payment Status Has Been Updated"
            intro = (
                "The payment status for your order is now "
                f"{order.get_payment_status_display()}."
            )
            detail = (
                f"Your order status is {_order_status_label(order.status)}."
            )
            next_step = (
                "Please contact the resort team if you have questions about "
                "your payment."
            )

        payment_note = (
            f"Payment status is now {order.get_payment_status_display()}."
            if (
                order.status != previous_status
                and order.payment_status != previous_payment_status
            )
            else ""
        )
        closing_text = (
            "Thank you for choosing Kalitusi Nature Resort. We look forward "
            "to serving you!\n\nWarm regards,\n\n"
            "Kalitusi Nature Resort\n"
            "Experience Nature. Experience Kalitusi."
        )
        closing_html = (
            '<tr><td style="padding-top:24px;color:#64736c;'
            'font-size:13px;line-height:1.7;">'
            "Thank you for choosing <strong>Kalitusi Nature Resort"
            "</strong>. We look forward to serving you!<br><br>"
            "Warm regards,<br><br><strong>Kalitusi Nature Resort</strong><br>"
            "<em>Experience Nature. Experience Kalitusi.</em>"
            "</td></tr>"
        )
        customer_parts = [
            heading,
            f"Hello {order.guest_name},",
            intro,
            detail,
        ]
        if payment_note:
            customer_parts.append(payment_note)
        customer_parts.extend(
            [
                _order_summary_text(order),
                f"What's Next?\n{next_step}",
                closing_text,
            ]
        )
        content_html = (
            f'<p style="line-height:1.6;">{escape(intro)}</p>'
            f'<p style="line-height:1.6;">{escape(detail)}</p>'
            + (
                f'<p style="line-height:1.6;">{escape(payment_note)}</p>'
                if payment_note
                else ""
            )
            + order_html
            + '<h2 style="margin:24px 0 12px;font-size:18px;color:#234b3b;">'
            "What's Next?</h2>"
            f'<p style="line-height:1.6;">{escape(next_step)}</p>'
        )
        _send_email(
            f"{heading} - {order.number}",
            "\n\n".join(customer_parts),
            [order.email],
            order.number,
            html_message=_email_layout(
                heading,
                f"Hello {order.guest_name},",
                content_html,
                closing_html=closing_html,
            ),
        )
    was_review_eligible = (
        previous_status == Order.Status.COMPLETED
        and previous_payment_status == Order.PaymentStatus.PAID
    )
    if order_is_review_eligible(order) and not was_review_eligible:
        send_order_review_invitation_email(order)
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
    customer_heading, customer_message, customer_html = (
        _reservation_customer_email(reservation)
    )
    details = (
        f"Reference: {reservation.reference}\n"
        f"Guest: {reservation.guest_name}\n"
        f"Stay: {reservation.lodge.name}, "
        f"{reservation.check_in} to {reservation.check_out}\n"
        f"Guests: {reservation.adults} adults, {reservation.children} children\n"
        f"Status: {reservation.get_status_display()}"
    )
    if reservation.email:
        _send_email(
            f"{customer_heading} ({reservation.reference})",
            customer_message,
            [reservation.email],
            reservation.reference,
            html_message=customer_html,
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


def notify_reservation_status(
    reservation: LodgeReservation,
    previous_status: str | None = None,
) -> None:
    reservation_html = _reservation_email_html(reservation)
    customer_heading, customer_message, customer_html = (
        _reservation_customer_email(reservation)
    )
    if reservation.email:
        _send_email(
            f"{customer_heading} ({reservation.reference})",
            customer_message,
            [reservation.email],
            reservation.reference,
            html_message=customer_html,
        )
    if (
        reservation.status == LodgeReservation.Status.COMPLETED
        and previous_status != LodgeReservation.Status.COMPLETED
        and reservation_is_review_eligible(reservation)
    ):
        send_reservation_review_invitation_email(reservation)
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


def send_payment_receipt_email(payment: Payment) -> bool:
    try:
        receipt = payment.receipt
    except PaymentReceipt.DoesNotExist:
        logger.error(
            "Payment receipt email not sent for transaction %s: receipt is missing.",
            payment.transaction_reference,
        )
        return False

    target = payment.order or payment.reservation
    if not target.email:
        logger.warning(
            "Payment receipt email not sent for transaction %s: customer email is missing.",
            payment.transaction_reference,
        )
        return False

    if payment.order_id:
        payment_type = "Order"
        booking_reference = payment.order.number
    else:
        payment_type = "Reservation"
        booking_reference = payment.reservation.reference
    payment_status = "Paid in full" if receipt.balance_ugx == 0 else "Partially paid"
    text = (
        f"Hello {target.guest_name},\n\n"
        "Thank you for your payment to Kalitusi Nature Resort. "
        "Your payment receipt is attached as a PDF.\n\n"
        f"Receipt number: {receipt.number}\n"
        f"{payment_type}: {booking_reference}\n"
        f"Payment: UGX {payment.amount_ugx:,}\n"
        f"Total paid: UGX {receipt.paid_to_date_ugx:,}\n"
        f"Outstanding balance: UGX {receipt.balance_ugx:,}\n"
        f"Payment status: {payment_status}\n\n"
        "Warm regards,\nKalitusi Nature Resort\n"
        "Experience Nature. Experience Kalitusi."
    )
    payment_details_html = _details_table_html(
        [
            ("Receipt Number", receipt.number),
            (payment_type, booking_reference),
            ("Payment", f"UGX {payment.amount_ugx:,}"),
            ("Total Paid", f"UGX {receipt.paid_to_date_ugx:,}"),
            ("Outstanding Balance", f"UGX {receipt.balance_ugx:,}"),
            ("Payment Status", payment_status),
        ]
    )
    content_html = (
        f'<p style="line-height:1.6;">Hello {escape(target.guest_name)},</p>'
        '<p style="line-height:1.6;">Thank you for your payment to '
        "Kalitusi Nature Resort. Your payment receipt is attached as a PDF.</p>"
        f"{payment_details_html}"
    )
    return _send_email(
        f"Payment receipt {receipt.number}",
        text,
        [target.email],
        payment.transaction_reference,
        html_message=_email_layout(
            "Your Kalitusi payment receipt",
            "Payment confirmed",
            content_html,
        ),
        attachments=[
            (
                f"{receipt.number}.pdf",
                bytes(receipt.pdf_data),
                "application/pdf",
            )
        ],
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
    closing_text = (
        "Best regards,\n\n"
        "Kalitusi Nature Resort\n"
        "Experience Nature. Experience Kalitusi."
    )
    closing_html = (
        '<tr><td style="padding-top:24px;color:#64736c;'
        'font-size:13px;line-height:1.7;">'
        "Best regards,<br><br><strong>Kalitusi Nature Resort</strong><br>"
        "<em>Experience Nature. Experience Kalitusi.</em>"
        "</td></tr>"
    )
    _send_email(
        "Reset Your Password",
        (
            "Reset Your Password\n\n"
            f"Hello {user.get_username()},\n\n"
            "We received a request to reset the password for your Kalitusi "
            "staff account.\n\n"
            "To create a new password, click the link below:\n\n"
            "Reset My Password:\n"
            f"{reset_url}\n\n"
            "If you did not request a password reset, you can safely ignore "
            "this email. Your account password will remain unchanged.\n\n"
            "For security reasons, please do not share this password reset "
            "link with anyone.\n\n"
            "If you need assistance, please contact the Kalitusi "
            "administration team.\n\n"
            f"{closing_text}"
        ),
        [user.email],
        f"password reset for staff account {user.pk}",
        html_message=_email_layout(
            "Reset Your Password",
            f"Hello {user.get_username()},",
            (
                '<p style="line-height:1.6;">We received a request to reset '
                "the password for your <strong>Kalitusi staff account</strong>."
                "</p>"
                '<p style="line-height:1.6;">To create a new password, click '
                "the button below:</p>"
                '<p style="margin:24px 0;text-align:center;">'
                f'<a href="{escape(reset_url, quote=True)}" '
                'style="display:inline-block;padding:13px 22px;'
                'background:#234b3b;color:#ffffff;text-decoration:none;'
                'border-radius:8px;font-weight:bold;">'
                "Reset My Password</a></p>"
                '<p style="line-height:1.6;">If you did not request a password '
                "reset, you can safely ignore this email. Your account "
                "password will remain unchanged.</p>"
                '<p style="line-height:1.6;">For security reasons, please do '
                "not share this password reset link with anyone.</p>"
                '<p style="line-height:1.6;">If you need assistance, please '
                "contact the Kalitusi administration team.</p>"
            ),
            closing_html=closing_html,
        ),
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
