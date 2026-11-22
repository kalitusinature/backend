import hashlib
import secrets
import uuid
from io import BytesIO

from django.conf import settings
from django.db.models import Sum
from django.utils import timezone
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    Image,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from .models import Payment, PaymentReceipt

FOREST = colors.HexColor("#174b3a")
TEAL = colors.HexColor("#008b92")
MUTED = colors.HexColor("#63746c")
PALE = colors.HexColor("#edf4ef")
GRID = colors.HexColor("#dce6e0")


def _money(amount: int) -> str:
    return f"UGX {amount:,}"


def _paragraph(value, style):
    from xml.sax.saxutils import escape

    return Paragraph(escape(str(value)), style)


def payment_totals(payment: Payment) -> tuple[int, int, int]:
    if payment.order_id:
        target = payment.order
        due = target.total_ugx
    else:
        target = payment.reservation
        due = target.total_due_ugx
    paid = (
        target.payments.filter(status=Payment.Status.VERIFIED).aggregate(
            total=Sum("amount_ugx")
        )["total"]
        or 0
    )
    return due, paid, max(due - paid, 0)


def _qr_drawing(verification_url: str, size: float = 22 * mm) -> Drawing:
    qr_code = QrCodeWidget(verification_url)
    bounds = qr_code.getBounds()
    width = bounds[2] - bounds[0]
    height = bounds[3] - bounds[1]
    drawing = Drawing(
        size,
        size,
        transform=[size / width, 0, 0, size / height, 0, 0],
    )
    drawing.add(qr_code)
    return drawing


def _build_pdf(
    payment: Payment,
    receipt_number: str,
    verification_token: str,
    due: int,
    paid: int,
    balance: int,
) -> bytes:
    target = payment.order or payment.reservation
    buffer = BytesIO()
    styles = getSampleStyleSheet()
    styles.add(
        ParagraphStyle(
            name="ReceiptTitle",
            parent=styles["Title"],
            alignment=TA_LEFT,
            textColor=FOREST,
            fontName="Helvetica-Bold",
            fontSize=22,
            leading=27,
            spaceAfter=2,
        )
    )
    styles.add(
        ParagraphStyle(
            name="ReceiptSubtitle",
            parent=styles["Normal"],
            textColor=MUTED,
            fontSize=9,
            leading=13,
        )
    )
    styles.add(
        ParagraphStyle(
            name="ReceiptSection",
            parent=styles["Heading2"],
            textColor=FOREST,
            fontName="Helvetica-Bold",
            fontSize=12,
            leading=15,
            spaceBefore=8,
            spaceAfter=6,
        )
    )
    styles.add(
        ParagraphStyle(
            name="ReceiptCell",
            parent=styles["BodyText"],
            fontSize=9,
            leading=12,
        )
    )
    styles.add(
        ParagraphStyle(
            name="ReceiptCellRight",
            parent=styles["ReceiptCell"],
            alignment=TA_RIGHT,
        )
    )
    styles.add(
        ParagraphStyle(
            name="ReceiptSmallCenter",
            parent=styles["ReceiptSubtitle"],
            alignment=TA_CENTER,
            fontSize=8,
        )
    )

    def paragraph(value, style="ReceiptCell"):
        return _paragraph(value, styles[style])

    logo_path = (
        settings.BASE_DIR
        / "core"
        / "static"
        / "core"
        / "Kalitusi_Nature_Resort_Sunset_Logo.png"
    )
    if not logo_path.is_file():
        raise FileNotFoundError(f"Receipt logo is missing: {logo_path}")
    logo = Image(str(logo_path), width=50 * mm, height=20 * mm, kind="proportional")

    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=15 * mm,
        bottomMargin=18 * mm,
        title=f"Payment Receipt {receipt_number}",
        author="Kalitusi Nature Resort",
    )
    story = [
        Table(
            [
                [
                    logo,
                    [
                        paragraph("KALITUSI NATURE RESORT", "ReceiptTitle"),
                        paragraph("WhatsApp: +256 772 957 573", "ReceiptSubtitle"),
                        paragraph("Experience Nature. Experience Kalitusi.", "ReceiptSubtitle"),
                    ],
                    [
                        paragraph("PAYMENT RECEIPT", "ReceiptSection"),
                        paragraph(receipt_number),
                        paragraph(
                            timezone.localtime(payment.verified_at).strftime(
                                "%d %B %Y, %H:%M"
                            ),
                            "ReceiptSubtitle",
                        ),
                    ],
                ]
            ],
            colWidths=[52 * mm, 78 * mm, 42 * mm],
            style=TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("ALIGN", (2, 0), (2, 0), "RIGHT"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
                    ("LINEBELOW", (0, 0), (-1, -1), 1, GRID),
                ]
            ),
        ),
        Spacer(1, 7 * mm),
        paragraph("PAYMENT CONFIRMED", "ReceiptSection"),
    ]

    reference = payment.order.number if payment.order_id else payment.reservation.reference
    details = [
        ("Customer", target.guest_name),
        ("Email", target.email or "Not provided"),
        ("Phone", target.phone),
        ("Order / reservation reference", reference),
    ]
    details_table = Table(
        [
            [paragraph(label), paragraph(value)]
            for label, value in details
        ],
        colWidths=[55 * mm, 117 * mm],
        style=TableStyle(
            [
                ("BACKGROUND", (0, 0), (0, -1), PALE),
                ("GRID", (0, 0), (-1, -1), 0.5, GRID),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ]
        ),
    )
    story.append(details_table)

    if payment.order_id:
        story.append(paragraph("ORDER DETAILS", "ReceiptSection"))
        item_rows = [
            [
                paragraph("Item"),
                paragraph("Quantity"),
                paragraph("Unit price", "ReceiptCellRight"),
                paragraph("Subtotal", "ReceiptCellRight"),
            ]
        ]
        for item in payment.order.items.all():
            item_rows.append(
                [
                    paragraph(item.product_name),
                    paragraph(str(item.quantity)),
                    paragraph(_money(item.unit_price_ugx), "ReceiptCellRight"),
                    paragraph(_money(item.line_total_ugx), "ReceiptCellRight"),
                ]
            )
        item_table = Table(
            item_rows,
            colWidths=[76 * mm, 22 * mm, 36 * mm, 38 * mm],
            repeatRows=1,
        )
        item_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), PALE),
                    ("TEXTCOLOR", (0, 0), (-1, 0), FOREST),
                    ("GRID", (0, 0), (-1, -1), 0.5, GRID),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("ALIGN", (1, 1), (1, -1), "CENTER"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 7),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                    ("TOPPADDING", (0, 0), (-1, -1), 7),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                ]
            )
        )
        story.append(item_table)
    else:
        reservation = payment.reservation
        nights = (reservation.check_out - reservation.check_in).days
        story.append(paragraph("RESERVATION DETAILS", "ReceiptSection"))
        reservation_rows = [
            ("Accommodation", reservation.lodge.name),
            ("Check-in", reservation.check_in.strftime("%d %B %Y")),
            ("Check-out", reservation.check_out.strftime("%d %B %Y")),
            ("Nights", str(nights)),
            ("Adults", str(reservation.adults)),
            ("Children", str(reservation.children)),
        ]
        story.append(
            Table(
                [
                    [paragraph(label), paragraph(value)]
                    for label, value in reservation_rows
                ],
                colWidths=[55 * mm, 117 * mm],
                style=TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (0, -1), PALE),
                        ("GRID", (0, 0), (-1, -1), 0.5, GRID),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("LEFTPADDING", (0, 0), (-1, -1), 8),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                        ("TOPPADDING", (0, 0), (-1, -1), 7),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                    ]
                ),
            )
        )

    story.extend(
        [
            paragraph("PAYMENT BREAKDOWN", "ReceiptSection"),
            Table(
                [
                    [paragraph("Payment date"), paragraph(timezone.localtime(payment.verified_at).strftime("%d %B %Y, %H:%M"))],
                    [paragraph("Payment method"), paragraph(payment.get_method_display())],
                    [paragraph("Transaction reference"), paragraph(payment.transaction_reference)],
                    [paragraph("Total due"), paragraph(_money(due), "ReceiptCellRight")],
                    [paragraph("This payment"), paragraph(_money(payment.amount_ugx), "ReceiptCellRight")],
                    [paragraph("Paid to date"), paragraph(_money(paid), "ReceiptCellRight")],
                    [paragraph("Outstanding balance"), paragraph(_money(balance), "ReceiptCellRight")],
                    [
                        paragraph("Payment status"),
                        paragraph("PAID IN FULL" if balance == 0 else "PARTIALLY PAID"),
                    ],
                ],
                colWidths=[95 * mm, 77 * mm],
                style=TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (0, -1), PALE),
                        ("GRID", (0, 0), (-1, -1), 0.5, GRID),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("ALIGN", (1, 3), (1, 6), "RIGHT"),
                        ("FONTNAME", (0, 4), (-1, 4), "Helvetica-Bold"),
                        ("TEXTCOLOR", (0, 7), (-1, 7), FOREST),
                        ("LEFTPADDING", (0, 0), (-1, -1), 8),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                        ("TOPPADDING", (0, 0), (-1, -1), 7),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                    ]
                ),
            ),
            Spacer(1, 8 * mm),
        ]
    )

    verify_url = (
        f"{settings.RECEIPT_VERIFY_BASE_URL.rstrip('/')}/{verification_token}/"
    )
    verification_table = Table(
        [
            [
                _qr_drawing(verify_url),
                [
                    paragraph("VERIFY THIS RECEIPT", "ReceiptSection"),
                    paragraph(
                        "Scan the QR code to confirm this receipt was issued by "
                        "Kalitusi Nature Resort. Verification does not reveal "
                        "customer or transaction details.",
                        "ReceiptSubtitle",
                    ),
                ],
            ]
        ],
        colWidths=[30 * mm, 142 * mm],
        style=TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("LINEABOVE", (0, 0), (-1, -1), 1, GRID),
            ]
        ),
    )
    story.extend(
        [
            verification_table,
            Spacer(1, 5 * mm),
            paragraph(
                "Thank you for choosing Kalitusi Nature Resort.",
                "ReceiptSmallCenter",
            ),
        ]
    )

    def draw_footer(canvas, document):
        canvas.saveState()
        canvas.setStrokeColor(GRID)
        canvas.line(18 * mm, 13 * mm, A4[0] - 18 * mm, 13 * mm)
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(MUTED)
        canvas.drawString(18 * mm, 8 * mm, "Experience Nature. Experience Kalitusi.")
        canvas.drawRightString(A4[0] - 18 * mm, 8 * mm, f"Page {document.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=draw_footer, onLaterPages=draw_footer)
    return buffer.getvalue()


def create_payment_receipt(payment: Payment) -> PaymentReceipt:
    if payment.status != Payment.Status.VERIFIED:
        raise ValueError("A receipt can only be created for a verified payment.")
    verification_token = secrets.token_urlsafe(32)
    due, paid, balance = payment_totals(payment)
    receipt = PaymentReceipt.objects.create(
        payment=payment,
        number=f"KR-RCPT-{uuid.uuid4().hex[:12].upper()}",
        verification_token_digest=hashlib.sha256(
            verification_token.encode("utf-8")
        ).hexdigest(),
        total_due_ugx=due,
        paid_to_date_ugx=paid,
        balance_ugx=balance,
        pdf_data=b"",
    )
    receipt.pdf_data = _build_pdf(
        payment,
        receipt.number,
        verification_token,
        due,
        paid,
        balance,
    )
    receipt.save(update_fields=["pdf_data"])
    return receipt


def find_verified_receipt(token: str) -> PaymentReceipt | None:
    token_digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return (
        PaymentReceipt.objects.select_related("payment", "payment__order", "payment__reservation")
        .filter(
            verification_token_digest=token_digest,
            payment__status=Payment.Status.VERIFIED,
        )
        .first()
    )
