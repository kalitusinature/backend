# Kalitusi Resort ordering backend

Django REST API and staff portal for the Odoo storefront. The backend stores menu data, dine-in/lodge food orders, and lodge/cottage reservation requests. It does not collect payments.

## Local development

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python manage.py makemigrations core
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Staff portal: `http://127.0.0.1:8000/staff/`

Django catalog administration: `http://127.0.0.1:8000/admin/`

Health check: `http://127.0.0.1:8000/health/`

Create categories, products, and lodges in Django admin before using the public API. Product prices are stored as whole UGX amounts. Lodge reservations are requests (`pending`) until staff confirms them. Only confirmed and checked-in stays reserve dates; multiple pending requests may overlap, and staff checks for conflicts when confirming.

Set `CLOUDINARY_NAME`, `CLOUDINARY_KEY`, and `CLOUDINARY_SECRET` in the local
`.env` file to enable product and lodge image uploads. Keep the Cloudinary API
secret out of source control.

## Neon PostgreSQL

Django reads `DATABASE_URL` from the root `.env` file or the deployment
environment. Neon provides a pooled `DATABASE_URL` for app traffic and a direct
`DATABASE_URL_UNPOOLED` for schema migrations. Keep both values secret and
never commit `.env`.

The Render Blueprint below still binds `DATABASE_URL` to its Render PostgreSQL
resource. Before switching production to Neon, migrate the existing database
data and then update the Render environment/Blueprint to use Neon; applying
Django migrations creates tables but does not copy catalog or order data.

## Render deployment

1. Push this project to a GitHub repository and create a Render Blueprint from `render.yaml`.
2. Set `DJANGO_SUPERUSER_USERNAME`, `DJANGO_SUPERUSER_EMAIL`, and `DJANGO_SUPERUSER_PASSWORD` in the Render service environment. Use a unique, strong password. The startup command creates this initial admin only when the username does not already exist.
3. Set `CLOUDINARY_NAME`, `CLOUDINARY_KEY`, and `CLOUDINARY_SECRET` in the
   Render service environment using values from your Cloudinary API settings.
4. Deploy. Render provisions PostgreSQL, runs migrations, collects static files, bootstraps the initial admin, and starts Gunicorn.
5. Open `/staff/` on the deployed service and add menu categories, products, and lodge/cottage inventory.
6. Add the final Render service origin to `CORS_ALLOWED_ORIGINS` and `CSRF_TRUSTED_ORIGINS` if it differs from the value in `render.yaml`.
7. Configure the Odoo frontend to use the deployed service base URL as `window.CJ_API_BASE_URL`. Do not put database credentials or Django secrets in Odoo.

Debug mode is always disabled on Render, even if its `DEBUG` environment variable is accidentally set to `true`.

The Blueprint uses a Render PostgreSQL database and allows browser API calls from `https://kalitusi-nature-resort.odoo.com` and `https://kalitusinature.github.io`. Update these origins if either site domain changes.

## Public API

All endpoints are under `/api/v1/`. JSON is expected for POST requests.

### Read menu

- `GET /api/v1/categories/`
- `GET /api/v1/products/`
- `GET /api/v1/products/?category=drinks`
- `GET /api/v1/lodges/`
- `GET /api/v1/lodges/?check_in=2027-01-10&check_out=2027-01-12&guests=2` returns stays that satisfy capacity, booking limits, check-in days, blockouts, and confirmed/in-house reservations.
- `GET /api/v1/lodges/<slug>/availability/?month=2027-01&nights=2&guests=2` returns available, booked, and unavailable check-in dates. Add `check_in=2027-01-10` to receive available, booked, and unavailable check-out dates for that arrival.

Only active categories, available products, and active lodges are public.

### Submit an order

`POST /api/v1/orders/`

```json
{
  "guest_name": "Guest Name",
  "phone": "+256700000000",
  "email": "guest@example.com",
  "order_type": "dine_in",
  "location": "Table 4",
  "payment_method": "cash",
  "note": "",
  "items": [
    {"product": "classic-smash-burger", "quantity": 2}
  ]
}
```

`order_type` is `dine_in` or `lodge`; `location` is the table number or lodge/cottage identifier. `payment_method` is `cash` or `card_at_counter`. `email` is optional; when omitted, the order is still saved for staff but cannot be tracked by the customer. The server looks up current prices and calculates totals; clients cannot set prices. The order remains unpaid until staff records payment at the resort.

Successful response (`201`) contains the order number, status, server-calculated UGX total, and creation time.

The Odoo menu and product pages load categories and available products from this API. Checkout posts orders to Render; the server validates product slugs and calculates prices from its catalog. New orders are stored in Render's database and appear in the GitHub Pages admin portal's Orders view, which refreshes every 30 seconds.

Customers who provide an email receive an order thank-you email and status updates. The operations inbox receives an alert for new orders and status changes. Order tracking expires two days after placement; verified payment receipts remain available through the customer receipt lookup.

`POST /api/v1/orders/track/` accepts an order number and email and returns the matching order's status and item totals. A non-matching pair returns `404`; an expired lookup returns `410`. The response excludes the guest name, phone, and email. The endpoint is rate-limited to 600 lookups per hour per client.

The standalone Odoo page templates in this repository, including `cottages.html`, `header.html`, `my-orders.html`, and `my-reservations.html`, must also be copied into and published from the Odoo Website editor; pushing them to GitHub does not update Odoo pages automatically.

### Request a lodge reservation

`POST /api/v1/reservations/`

```json
{
  "lodge": "lake-cottage",
  "guest_name": "Guest Name",
  "email": "guest@example.com",
  "phone": "+256700000000",
  "check_in": "2027-01-10",
  "check_out": "2027-01-12",
  "adults": 2,
  "children": 1,
  "occasion": "",
  "note": ""
}
```

New requests are `pending`; a `201` response includes a unique reservation reference and booking status. Email is required. The reservation page provides a calendar range picker for check-in and check-out. The server validates stay dates, lodge capacity and policies, blockouts, and confirmed/in-house overlaps. Pending requests remain visible to staff but do not appear as booked or block availability; staff checks for conflicts when confirming. Customers and the operations inbox receive reservation emails when requests are created and when staff changes a reservation status.

The public form does not set reservation prices; a new reservation's total due is UGX 0. In the staff Reservations page, use **Edit details** to set the total before **Record payment** appears. Staff records each received payment with its transaction or counter receipt reference. The separate reservation review invitation is sent after check-out, regardless of payment.

The customer reservation form is the Odoo `/reservation` page. The Odoo cottages page's **Book Now** links open that page and pass the lodge slug in the `lodge` query parameter. The reservation page uses the shared Odoo header and sends availability and reservation requests to the production API at `https://api.kalitusi.com/api/v1`. Its **Back to cottages** links return to `/cottages`. Publish the updated `cottages.html` and `reservation.html` templates in Odoo.

## Staff portal

`/staff/` is for authenticated staff accounts. It shows recent orders and reservations, supports status changes, and links to catalog management:

- Orders: open order details, confirm, prepare, mark ready/delivered, cancel with a required reason sent to the customer, or record a verified full/partial payment using its transaction or counter receipt reference. Order statuses can only move forward through the service flow; delivered orders retain the backend `completed` status.
- Reservations: create manually, search/filter, paginate, set the total due, edit details, record verified payments, confirm, decline, cancel with a required reason sent in the customer apology email, complete, or delete inactive records. Day/week/month calendar views show the status of each stay.
- Payment receipts: search and filter verified payments, view/print or download PDFs, resend customer copies, and review the original transaction reference.
- Products: create and maintain food/drink items, UGX price, uploaded Cloudinary image, availability, and featured state.
- Categories: create menu sections and control order/visibility.
- Lodges: maintain lodge/cottage details, capacity, images, stay length and advance-booking rules, check-in days, and active state. Add and remove unavailable date ranges.

Use Django admin for staff user creation and detailed record management. It is not a payment-verification surface; authenticated staff record received payments in the React portal.

## Payment receipts

The current payment verification source is an authenticated staff member recording a payment received at the resort. Each record requires a unique transaction or counter receipt reference. The API rejects duplicate references (case-insensitively), prevents overpayment, and creates a receipt only for a verified payment. Orders use their server-calculated total; staff enters a reservation's total due. Multiple verified payments create separate receipts with the amount paid, paid-to-date total, and remaining balance.

PDF receipts are A4 documents with the resort logo, business name and public WhatsApp contact, customer and booking details, item or stay details, payment method/reference, status, and a QR verification code. PDF bytes are stored in PostgreSQL and are not exposed through public file URLs. Staff downloads require staff authentication; customer downloads require the matching booking reference and email. Public QR verification returns only the receipt number, verified status, amount, and issue time.

The payment confirmation email includes the PDF attachment through either Resend or SMTP. If delivery fails, the payment and receipt remain saved; staff can retry delivery from Payment Receipts. Customers can retrieve order receipts from My Orders and reservation details/receipts from My Reservations.

The receipt PDFs use the backend-bundled logo in `core/static/core/Kalitusi_Nature_Resort_Sunset_Logo.png` and the public WhatsApp number `+256 772 957 573`; no address or tax identifier is configured. `RECEIPT_VERIFY_BASE_URL` can override the default public verification endpoint.

## React admin portal

The React admin portal is a separate GitHub Pages app. Its API base URL is
`https://api.kalitusi.com/api/v1` (configurable via `VITE_API_BASE_URL` in the
admin portal). The Django staff API uses token authentication and only accepts
active staff accounts.

The portal uses these protected endpoints after login:

- `POST /auth/token/` and `POST /auth/logout/`
- `GET /staff/push/vapid-public-key/` and `POST, DELETE /staff/push/subscriptions/`
- `GET /staff/dashboard/`
- `GET /staff/orders/` and `PATCH /staff/orders/<id>/`
- `POST /staff/payments/`
- `GET /staff/payment-receipts/`, `GET /staff/payment-receipts/<payment-id>/download/`, and `POST /staff/payment-receipts/<payment-id>/resend/`
- `GET, POST /staff/products/` and `GET, PATCH /staff/products/<id>/`
- `GET, POST /staff/categories/` and `GET, PATCH /staff/categories/<id>/`
- `GET, POST /staff/lodges/` and `GET, PATCH /staff/lodges/<id>/`
- `GET, POST /staff/reservations/`, `GET, PATCH, DELETE /staff/reservations/<id>/`, and `GET /staff/reservations/calendar/`
- `GET, POST /staff/lodge-blockouts/` and `GET, PATCH, DELETE /staff/lodge-blockouts/<id>/`

Customer receipt lookups are `POST /orders/receipts/` and `POST /reservations/track/`. Customer PDF downloads use the corresponding `/orders/receipts/download/` and `/reservations/receipts/download/` endpoints; both require a matching booking reference and email in the request body. `GET /receipts/verify/<opaque-token>/` is limited to receipt verification metadata.

Product and lodge review summaries are public at
`GET /products/<slug>/reviews/` and `GET /lodges/<slug>/reviews/`. Review
submissions use `GET, POST /reviews/access/` with a signed link token; links are
issued only for completed, fully paid orders or checked-out stays.
Set `CUSTOMER_REVIEW_PAGE_URL` only if you need to override the published
customer-review page URL. The default is
`https://www.kalitusi.com/customer-review`. A separate review
invitation email is sent as soon as an order is both completed and fully paid,
or a stay is checked out. Clicking an email star opens the
private review page with that rating preselected; customers can add a comment
and submit there without signing in. Verified order or reservation pages also
provide eligible private links.

Uploaded product images are optimized in the browser and sent to Cloudinary by
the authenticated backend. PostgreSQL stores the secure delivery URL and asset
ID; existing image URLs remain supported.

Set `CORS_ALLOWED_ORIGINS` on Render to include both the Odoo storefront origin
and each deployed admin portal origin, such as `https://admin.kalitusi.com`.
Create the first staff account using
`DJANGO_SUPERUSER_USERNAME`, `DJANGO_SUPERUSER_EMAIL`, and
`DJANGO_SUPERUSER_PASSWORD` in Render's environment settings. Never put these
credentials in the React app or its GitHub repository.

To send transactional emails through Resend on Render, verify `kalitusi.com`
in Resend and configure its HTTPS API:

- `RESEND_API_KEY` to a Resend API key (keep this secret). For existing
  deployments, `EMAIL_HOST_PASSWORD` is accepted as a fallback.
- `EMAIL_TIMEOUT=10`
- `DEFAULT_FROM_EMAIL` to a sender on the verified domain, such as
  `notifications@kalitusi.com`
- `STAFF_NOTIFICATION_EMAIL` to the operations inbox

`RESERVATION_NOTIFICATION_EMAIL` is still accepted as a fallback for
`STAFF_NOTIFICATION_EMAIL`. Keep all credentials in Render environment
settings, never in the repository. If the Resend API is unavailable, records
are still saved and the backend logs the email delivery failure.
Order and reservation emails include HTML tables, the resort logo, and product
or lodge images when available, alongside a plain-text alternative. Verified
payment confirmation emails attach the generated PDF receipt. Customer order
updates use distinct messages for confirmation, preparation, readiness, and
completion. Review links are included only after an order is completed and paid
or a stay is checked out.

The admin login includes a password-reset flow for active staff accounts. A
reset link is sent only to the email address saved on the staff account; set
`ADMIN_PORTAL_URL` if the admin portal is hosted somewhere other than
`https://admin.kalitusi.com`. The reset request response does not reveal
whether an account exists. Reset emails use a branded HTML button and include
a plain-text alternative. There is no public staff signup; creating a staff
account in the portal alerts the operations inbox without emailing its
temporary password.

### Background push notifications

The admin portal sends new-order and pending-reservation alerts to every active
staff account's opted-in devices. Web Push requires a VAPID key pair. Generate
one locally after installing the requirements:

```powershell
python -c "import base64; from py_vapid import Vapid; from cryptography.hazmat.primitives import serialization as s; v=Vapid(); v.generate_keys(); print('VAPID_PUBLIC_KEY='+base64.urlsafe_b64encode(v.public_key.public_bytes(s.Encoding.X962,s.PublicFormat.UncompressedPoint)).decode().rstrip('=')); print('VAPID_PRIVATE_KEY='+base64.urlsafe_b64encode(v.private_key.private_bytes(s.Encoding.DER,s.PrivateFormat.PKCS8,s.NoEncryption())).decode().rstrip('='))"
```

In the Render backend service environment, set `VAPID_PUBLIC_KEY` to the
printed public value, `VAPID_PRIVATE_KEY` to the printed private value, and
`VAPID_SUBJECT` to a monitored contact such as `mailto:operations@your-domain`.
Store the private value only in Render's secret environment settings; never
commit it or paste it into frontend code. Keep the same key pair across
deployments. If the keys are rotated, staff devices must opt in again.

After redeploying, each staff member must sign in to the HTTPS admin portal and
select **Enable push** and allow notifications. The browser stores a
subscription for that staff account/device; disabling push removes that
device's subscription. On iOS/iPadOS, install the portal to the Home Screen
before enabling Web Push. Push delivery is subject to browser/OS notification
settings and the device's internet connection.
