from django.shortcuts import render


def reservation_page(request):
    return render(
        request,
        "reservation.html",
        {"api_base_url": request.build_absolute_uri("/api/v1").rstrip("/")},
    )
