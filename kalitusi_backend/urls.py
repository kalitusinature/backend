from django.contrib import admin
from django.urls import include, path
from django.http import JsonResponse

from core.page_views import reservation_page


def health(request):
    return JsonResponse({"status": "ok"})


urlpatterns = [
    path("health/", health, name="health"),
    path("reservation/", reservation_page, name="reservation_page"),
    path("admin/", admin.site.urls),
    path("staff/", include("core.staff_urls")),
    path("api/v1/", include("core.api_urls")),
]
