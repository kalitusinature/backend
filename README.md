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

Create categories, products, and lodges in Django admin before using the public API. Product prices are stored as whole UGX amounts. Lodge reservations are requests (`pending`) until staff confirms them. Pending and confirmed stays both reserve their dates; overlapping requests are rejected under a per-lodge database lock.

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
- `GET /api/v1/lodges/?check_in=2027-01-10&check_out=2027-01-12&guests=2` returns stays that satisfy capacity, booking limits, check-in days, blockouts, and pending/confirmed reservations.

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

Customers who provide an email are sent to the Odoo My Orders page after checkout, where the page checks for status/payment changes while open. Online tracking expires two days after the order is placed. No email notification is sent.

`POST /api/v1/orders/track/` accepts an order number and email and returns the matching order's status and item totals. A non-matching pair returns `404`; an expired lookup returns `410`. The response excludes the guest name, phone, and email. The endpoint is rate-limited to 600 lookups per hour per client.

The standalone Odoo page templates in this repository, including `cottages.html` and `my-orders.html`, must also be copied into and published from the Odoo Website editor; pushing them to GitHub does not update Odoo pages automatically.

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

New requests are `pending`; a `201` response includes a unique reservation reference and booking status. Email is required. The server validates stay dates, lodge capacity and policies, blockouts, and pending/confirmed overlaps. Reservation notifications use Django's SMTP backend when configured.

The customer reservation form is hosted by Django at `/reservation/` on the backend domain. The Odoo cottages page's **Book Now** links open that backend page and pass the lodge slug in the `lodge` query parameter. There is no need to create a `/reservation` page in Odoo. After updating `cottages.html`, publish that template in Odoo so its links point to the backend form.

## Staff portal

`/staff/` is for authenticated staff accounts. It shows recent orders and reservations, supports status changes, and links to catalog management:

- Orders: open order details, confirm, prepare, mark ready/delivered, cancel, or mark payment received. Order statuses can only move forward through the service flow; delivered orders retain the backend `completed` status.
- Reservations: create manually, search/filter, paginate, edit details, confirm, decline, cancel, complete, or delete inactive records. Day/week/month calendar views show the status of each stay.
- Products: create and maintain food/drink items, UGX price, uploaded Cloudinary image, availability, and featured state.
- Categories: create menu sections and control order/visibility.
- Lodges: maintain lodge/cottage details, capacity, images, stay length and advance-booking rules, check-in days, and active state. Add and remove unavailable date ranges.

Use Django admin for staff user creation and detailed record management. Payment processing is intentionally not implemented.

## React admin portal

The React admin portal is a separate GitHub Pages app. Its API base URL is
`https://backend-bte7.onrender.com/api/v1`. The Django staff API uses token
authentication and only accepts active staff accounts.

The portal uses these protected endpoints after login:

- `POST /auth/token/` and `POST /auth/logout/`
- `GET /staff/dashboard/`
- `GET /staff/orders/` and `PATCH /staff/orders/<id>/`
- `GET, POST /staff/products/` and `GET, PATCH /staff/products/<id>/`
- `GET, POST /staff/categories/` and `GET, PATCH /staff/categories/<id>/`
- `GET, POST /staff/lodges/` and `GET, PATCH /staff/lodges/<id>/`
- `GET, POST /staff/reservations/`, `GET, PATCH, DELETE /staff/reservations/<id>/`, and `GET /staff/reservations/calendar/`
- `GET, POST /staff/lodge-blockouts/` and `GET, PATCH, DELETE /staff/lodge-blockouts/<id>/`

Uploaded product images are optimized in the browser and sent to Cloudinary by
the authenticated backend. PostgreSQL stores the secure delivery URL and asset
ID; existing image URLs remain supported.

Set `CORS_ALLOWED_ORIGINS` on Render to include both the Odoo storefront origin
and `https://kalitusinature.github.io`. Create the first staff account using
`DJANGO_SUPERUSER_USERNAME`, `DJANGO_SUPERUSER_EMAIL`, and
`DJANGO_SUPERUSER_PASSWORD` in Render's environment settings. Never put these
credentials in the React app or its GitHub repository.

To enable reservation emails on Render, configure `EMAIL_HOST`, `EMAIL_PORT`,
`EMAIL_USE_TLS`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, and
`DEFAULT_FROM_EMAIL`. Set `RESERVATION_NOTIFICATION_EMAIL` to receive new
reservation alerts. SMTP credentials must be stored only in Render's
environment settings; without SMTP configuration, reservations are still
saved and the backend logs that delivery was skipped.
