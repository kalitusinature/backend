from django.contrib import admin
from django.urls import include, path
from django.http import JsonResponse


def health(request):
    return JsonResponse({"status": "ok"})


urlpatterns = [
    path("health/", health, name="health"),
    path("admin/", admin.site.urls),
    path("staff/", include("core.staff_urls")),
    path("api/v1/", include("core.api_urls")),
]
