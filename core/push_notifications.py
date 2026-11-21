import json
import logging

from django.conf import settings
from pywebpush import WebPushException, webpush

from .models import LodgeReservation, Order, StaffPushSubscription

logger = logging.getLogger(__name__)


def _send_to_staff(title, body, tag, page):
    subscriptions = StaffPushSubscription.objects.filter(
        user__is_active=True,
        user__is_staff=True,
    )
    if not subscriptions.exists():
        return
    if not all(
        (
            settings.VAPID_PUBLIC_KEY,
            settings.VAPID_PRIVATE_KEY,
            settings.VAPID_SUBJECT,
        )
    ):
        logger.warning("Web Push is not configured; no staff push was sent.")
        return

    payload = json.dumps(
        {
            "title": title,
            "body": body,
            "tag": tag,
            "page": page,
            "url": "/",
        }
    )
    for subscription in subscriptions.iterator():
        try:
            webpush(
                subscription_info={
                    "endpoint": subscription.endpoint,
                    "keys": {
                        "p256dh": subscription.p256dh,
                        "auth": subscription.auth,
                    },
                },
                data=payload,
                vapid_private_key=settings.VAPID_PRIVATE_KEY,
                vapid_claims={"sub": settings.VAPID_SUBJECT},
                ttl=3600,
                timeout=10,
            )
        except WebPushException as error:
            response = error.response
            status_code = response.status_code if response is not None else None
            if status_code in (404, 410):
                subscription_id = subscription.pk
                subscription.delete()
                logger.info(
                    "Removed expired staff push subscription %s.",
                    subscription_id,
                )
            else:
                logger.warning(
                    "Web Push delivery failed for staff subscription %s (status %s).",
                    subscription.pk,
                    status_code,
                    exc_info=True,
                )


def notify_new_order(order):
    _send_to_staff(
        "New food order",
        f"Order {order.number} is waiting for staff review.",
        f"order-{order.pk}",
        "orders",
    )


def notify_new_reservation(reservation):
    if reservation.status != LodgeReservation.Status.PENDING:
        return
    _send_to_staff(
        "New reservation request",
        f"Reservation {reservation.reference} is awaiting review.",
        f"reservation-{reservation.pk}",
        "reservations",
    )
