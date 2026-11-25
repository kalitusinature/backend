import os
from pathlib import Path

import dj_database_url
from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-change-this-secret-key")
DEBUG = (
    os.environ.get("DEBUG", "true").lower() == "true"
    and os.environ.get("RENDER") != "true"
)
CLOUDINARY_NAME = os.environ.get("CLOUDINARY_NAME", "").strip()
CLOUDINARY_KEY = os.environ.get("CLOUDINARY_KEY", "").strip()
CLOUDINARY_SECRET = os.environ.get("CLOUDINARY_SECRET", "").strip()
ALLOWED_HOSTS = [
    "api.kalitusi.com",
    "backend-bte7.onrender.com",
    "localhost",
    "127.0.0.1",
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework.authtoken",
    "corsheaders",
    "rest_framework",
    "core.apps.CoreConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "kalitusi_backend.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    }
]
WSGI_APPLICATION = "kalitusi_backend.wsgi.application"
ASGI_APPLICATION = "kalitusi_backend.asgi.application"

DATABASE_URL = os.environ.get("DATABASE_URL")
if DATABASE_URL:
    database_config = dj_database_url.parse(DATABASE_URL, conn_max_age=600)
    database_config["CONN_HEALTH_CHECKS"] = True
    if "-pooler" in (database_config.get("HOST") or ""):
        database_config["DISABLE_SERVER_SIDE_CURSORS"] = True
    DATABASES = {"default": database_config}
elif DEBUG:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }
else:
    raise ImproperlyConfigured("DATABASE_URL must be set when DEBUG is false.")

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Africa/Kampala"
USE_I18N = True
USE_TZ = True
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
    },
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "staff_login"
LOGIN_REDIRECT_URL = "staff_dashboard"
LOGOUT_REDIRECT_URL = "staff_login"

CORS_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get(
        "CORS_ALLOWED_ORIGINS",
        "https://admin.kalitusi.com,https://www.kalitusi.com,https://kalitusi-nature-resort.odoo.com,https://kalitusinature.github.io",
    ).split(",")
    if origin.strip()
]
if "https://kalitusinature.github.io" not in CORS_ALLOWED_ORIGINS:
    CORS_ALLOWED_ORIGINS.append("https://kalitusinature.github.io")
if "https://admin.kalitusi.com" not in CORS_ALLOWED_ORIGINS:
    CORS_ALLOWED_ORIGINS.append("https://admin.kalitusi.com")
if DEBUG:
    for origin in ("http://localhost:5173", "http://127.0.0.1:5173"):
        if origin not in CORS_ALLOWED_ORIGINS:
            CORS_ALLOWED_ORIGINS.append(origin)
CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get(
        "CSRF_TRUSTED_ORIGINS",
        "https://admin.kalitusi.com,https://www.kalitusi.com,https://kalitusi-nature-resort.odoo.com,https://kalitusinature.github.io",
    ).split(",")
    if origin.strip()
]
if "https://admin.kalitusi.com" not in CSRF_TRUSTED_ORIGINS:
    CSRF_TRUSTED_ORIGINS.append("https://admin.kalitusi.com")
CORS_URLS_REGEX = r"^/api/.*$"

REST_FRAMEWORK = {
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.ScopedRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": "300/hour",
        "order_create": "30/hour",
        "order_track": "600/hour",
        "order_history_code": "10/hour",
        "order_history_verify": "20/hour",
        "chat_code_request": "10/hour",
        "chat_code_verify": "20/hour",
        "chat_messages": "60/hour",
        "support_ticket_create": "10/hour",
        "review_create": "20/hour",
        "reservation_create": "10/hour",
        "staff_login": "10/hour",
        "staff_password_reset_request": "5/hour",
        "staff_password_reset_confirm": "10/hour",
        "receipt_lookup": "20/hour",
        "receipt_verification": "60/hour",
    },
}

ADMIN_PORTAL_URL = os.environ.get(
    "ADMIN_PORTAL_URL", "https://admin.kalitusi.com"
).strip().rstrip("/")
EMAIL_HOST = os.environ.get("EMAIL_HOST", "").strip()
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
EMAIL_USE_TLS = os.environ.get("EMAIL_USE_TLS", "true").lower() == "true"
EMAIL_TIMEOUT = int(os.environ.get("EMAIL_TIMEOUT", "10"))
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "").strip()
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
RESEND_API_KEY = (
    os.environ.get("RESEND_API_KEY", "").strip()
    or EMAIL_HOST_PASSWORD.strip()
)
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", EMAIL_HOST_USER).strip()
SUPPORT_TICKET_EMAIL = os.environ.get(
    "SUPPORT_TICKET_EMAIL", "kalitusinature@gmail.com"
).strip()
CUSTOMER_REVIEW_PAGE_URL = os.environ.get(
    "CUSTOMER_REVIEW_PAGE_URL",
    "https://kalitusi.com/customer-review",
).strip()
STAFF_NOTIFICATION_EMAIL = (
    os.environ.get("STAFF_NOTIFICATION_EMAIL", "").strip()
    or os.environ.get("RESERVATION_NOTIFICATION_EMAIL", "").strip()
)
RESERVATION_NOTIFICATION_EMAIL = STAFF_NOTIFICATION_EMAIL
VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY", "").strip()
VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "").strip()
VAPID_SUBJECT = os.environ.get("VAPID_SUBJECT", "").strip()
RECEIPT_VERIFY_BASE_URL = os.environ.get(
    "RECEIPT_VERIFY_BASE_URL",
    "https://api.kalitusi.com/api/v1/receipts/verify",
).strip().rstrip("/")

if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
