from urllib.parse import urlencode, urlsplit, urlunsplit

from django.conf import settings
from django.core import signing

from .models import (
    CustomerReview,
    LodgeReservation,
    Order,
    Product,
)

_REVIEW_LINK_SALT = "core.customer-review-link"


def order_is_review_eligible(order):
    return (
        order.status == Order.Status.COMPLETED
        and order.payment_status == Order.PaymentStatus.PAID
        and order.total_ugx > 0
    )


def reservation_is_review_eligible(reservation):
    return reservation.status == LodgeReservation.Status.COMPLETED


def product_review_token(order, product):
    return signing.dumps(
        {"kind": "product", "order_id": order.pk, "product_id": product.pk},
        salt=_REVIEW_LINK_SALT,
        compress=True,
    )


def lodge_review_token(reservation):
    return signing.dumps(
        {
            "kind": "lodge",
            "reservation_id": reservation.pk,
            "lodge_id": reservation.lodge_id,
        },
        salt=_REVIEW_LINK_SALT,
        compress=True,
    )


def review_page_url(token, *, rating=None):
    params = {"token": token}
    if rating is not None:
        params["rating"] = rating
    page_url = urlsplit(settings.CUSTOMER_REVIEW_PAGE_URL)
    query = "&".join(
        value for value in (page_url.query, urlencode(params)) if value
    )
    return urlunsplit(page_url._replace(query=query))


def _unreviewed_order_products(order):
    if not order_is_review_eligible(order):
        return []

    reviewed_product_ids = set(
        CustomerReview.objects.filter(order=order, product__isnull=False).values_list(
            "product_id",
            flat=True,
        )
    )
    products = []
    seen_product_ids = set()
    for item in order.items.select_related("product").all():
        product = item.product
        if (
            product is None
            or product.pk in seen_product_ids
            or product.pk in reviewed_product_ids
        ):
            continue
        seen_product_ids.add(product.pk)
        products.append(product)
    return products


def order_review_links(order):
    return [
        {
            "product_name": product.name,
            "url": review_page_url(product_review_token(order, product)),
        }
        for product in _unreviewed_order_products(order)
    ]


def order_review_invitation_links(order):
    links = []
    for product in _unreviewed_order_products(order):
        token = product_review_token(order, product)
        links.append(
            {
                "product_name": product.name,
                "url": review_page_url(token),
                "rating_urls": {
                    rating: review_page_url(token, rating=rating)
                    for rating in range(1, 6)
                },
            }
        )
    return links


def _eligible_reservation_review_token(reservation):
    if (
        not reservation_is_review_eligible(reservation)
        or CustomerReview.objects.filter(reservation=reservation).exists()
    ):
        return None
    return lodge_review_token(reservation)


def reservation_review_invitation_links(reservation):
    token = _eligible_reservation_review_token(reservation)
    if not token:
        return None
    return {
        "url": review_page_url(token),
        "rating_urls": {
            rating: review_page_url(token, rating=rating)
            for rating in range(1, 6)
        },
    }


def reservation_review_url(reservation):
    token = _eligible_reservation_review_token(reservation)
    return review_page_url(token) if token else None


def resolve_review_link(token, *, for_update=False):
    try:
        payload = signing.loads(token, salt=_REVIEW_LINK_SALT)
        if not isinstance(payload, dict):
            return None
        if payload.get("kind") == "product":
            order_query = Order.objects
            if for_update:
                order_query = order_query.select_for_update()
            order = order_query.get(pk=payload["order_id"])
            product = Product.objects.get(pk=payload["product_id"])
            if (
                not order_is_review_eligible(order)
                or not order.items.filter(product=product).exists()
            ):
                return None
            return {
                "kind": "product",
                "target": product,
                "order": order,
                "review": CustomerReview.objects.filter(
                    order=order,
                    product=product,
                ).first(),
            }
        if payload.get("kind") == "lodge":
            reservation_query = LodgeReservation.objects
            if for_update:
                reservation_query = reservation_query.select_for_update()
            reservation = reservation_query.select_related("lodge").get(
                pk=payload["reservation_id"]
            )
            if (
                reservation.lodge_id != payload["lodge_id"]
                or not reservation_is_review_eligible(reservation)
            ):
                return None
            return {
                "kind": "lodge",
                "target": reservation.lodge,
                "reservation": reservation,
                "review": CustomerReview.objects.filter(
                    reservation=reservation,
                    lodge=reservation.lodge,
                ).first(),
            }
    except (
        KeyError,
        LodgeReservation.DoesNotExist,
        Order.DoesNotExist,
        Product.DoesNotExist,
        TypeError,
        ValueError,
        signing.BadSignature,
    ):
        return None
    return None
