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

Create categories, products, and lodges in Django admin before using the public API. Product prices are stored as whole UGX amounts. Lodge reservations are requests (`pending`) until staff confirms them. A reservation can only be confirmed if no other confirmed reservation overlaps the requested dates.

Set `CLOUDINARY_NAME`, `CLOUDINARY_KEY`, and `CLOUDINARY_SECRET` in the local
`.env` file to enable product image uploads. Keep the Cloudinary API secret out
of source control.

## Render deployment

1. Push this project to a GitHub repository and create a Render Blueprint from `render.yaml`.
2. Set `DJANGO_SUPERUSER_USERNAME`, `DJANGO_SUPERUSER_EMAIL`, and `DJANGO_SUPERUSER_PASSWORD` in the Render service environment. Use a unique, strong password. The startup command creates this initial admin only when the username does not already exist.
3. Set `CLOUDINARY_NAME`, `CLOUDINARY_KEY`, and `CLOUDINARY_SECRET` in the
   Render service environment using values from your Cloudinary API settings.
4. Deploy. Render provisions PostgreSQL, runs migrations, collects static files, bootstraps the initial admin, and starts Gunicorn.
5. Open `/staff/` on the deployed service and add menu categories, products, and lodge/cottage inventory.
6. Add the final Render service origin to `CORS_ALLOWED_ORIGINS` and `CSRF_TRUSTED_ORIGINS` if it differs from the value in `render.yaml`.
7. Configure the Odoo frontend to use the deployed service base URL (no trailing slash) as `window.CJ_API_BASE`. Do not put database credentials or Django secrets in Odoo.

The Blueprint uses a Render PostgreSQL database and allows browser API calls only from `https://kalitusi-nature-resort.odoo.com`. Update that origin if the Odoo site domain changes.

## Public API

All endpoints are under `/api/v1/`. JSON is expected for POST requests.

### Read menu

- `GET /api/v1/categories/`
- `GET /api/v1/products/`
- `GET /api/v1/products/?category=drinks`
- `GET /api/v1/lodges/`
- `GET /api/v1/lodges/?check_in=2027-01-10&check_out=2027-01-12` returns lodges without confirmed overlapping reservations.

Only active categories, available products, and active lodges are public.

### Submit an order

`POST /api/v1/orders/`

```json
{
  "guest_name": "Guest Name",
  "phone": "+256700000000",
  "order_type": "dine_in",
  "location": "Table 4",
  "payment_method": "cash",
  "note": "",
  "items": [
    {"product": "classic-smash-burger", "quantity": 2}
  ]
}
```

`order_type` is `dine_in` or `lodge`; `location` is the table number or lodge/cottage identifier. `payment_method` is `cash` or `card_at_counter`. The server looks up current prices and calculates totals; clients cannot set prices. The order remains unpaid until staff records payment at the resort.

Successful response (`201`) contains the order number, status, server-calculated UGX total, and creation time.

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
  "note": ""
}
```

New requests are `pending`; a `201` response includes a reservation reference. Staff review requests in the portal and confirm availability before accepting.

## Staff portal

`/staff/` is for authenticated staff accounts. It shows recent orders and reservations, supports status changes, and links to catalog management:

- Orders: accept, prepare, mark ready/completed, or cancel.
- Reservations: confirm, decline, or cancel; conflicting confirmed stays cannot be approved.
- Products: create and maintain food/drink items, UGX price, uploaded Cloudinary image, availability, and featured state.
- Categories: create menu sections and control order/visibility.
- Lodges: maintain lodge/cottage details, capacity, image, and active state.

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
- `GET /staff/reservations/` and `PATCH /staff/reservations/<id>/`

Uploaded product images are optimized in the browser and sent to Cloudinary by
the authenticated backend. PostgreSQL stores the secure delivery URL and asset
ID; existing image URLs remain supported.

Set `CORS_ALLOWED_ORIGINS` on Render to include both the Odoo storefront origin
and `https://kalitusinature.github.io`. Create the first staff account using
`DJANGO_SUPERUSER_USERNAME`, `DJANGO_SUPERUSER_EMAIL`, and
`DJANGO_SUPERUSER_PASSWORD` in Render's environment settings. Never put these
credentials in the React app or its GitHub repository.
