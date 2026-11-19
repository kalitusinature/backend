from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .models import Lodge, LodgeBlockout, LodgeReservation

ACTIVE_RESERVATION_STATUSES = (
    LodgeReservation.Status.PENDING,
    LodgeReservation.Status.CONFIRMED,
)


def validate_lodge_stay(
    *,
    lodge: Lodge,
    check_in,
    check_out,
    adults: int,
    children: int,
    status: str,
    exclude_reservation_id: int | None = None,
) -> None:
    errors = {}
    if check_out <= check_in:
        errors["check_out"] = "Check-out must be after check-in."
    else:
        nights = (check_out - check_in).days
        if nights < lodge.min_nights:
            errors["check_out"] = f"This stay requires at least {lodge.min_nights} night(s)."
        elif nights > lodge.max_nights:
            errors["check_out"] = f"This stay allows at most {lodge.max_nights} night(s)."

    if adults + children > lodge.capacity:
        errors["adults"] = "Guest count exceeds this lodge's capacity."

    if status in ACTIVE_RESERVATION_STATUSES:
        today = timezone.localdate()
        if check_in < today:
            errors["check_in"] = "Check-in cannot be in the past."
        else:
            advance_days = (check_in - today).days
            if advance_days < lodge.min_advance_days:
                errors["check_in"] = (
                    f"Reservations must be made at least "
                    f"{lodge.min_advance_days} day(s) in advance."
                )
            elif advance_days > lodge.max_advance_days:
                errors["check_in"] = (
                    f"Reservations can only be made up to "
                    f"{lodge.max_advance_days} day(s) in advance."
                )

        if (
            lodge.checkin_weekdays
            and check_in.weekday() not in lodge.checkin_weekdays
        ):
            errors["check_in"] = "This lodge is not available for check-in on that day."

        if check_out > check_in:
            if LodgeBlockout.objects.filter(
                lodge=lodge,
                start_date__lt=check_out,
                end_date__gt=check_in,
            ).exists():
                errors["check_in"] = "This lodge is unavailable for part of those dates."

            overlapping = LodgeReservation.objects.filter(
                lodge=lodge,
                status__in=ACTIVE_RESERVATION_STATUSES,
                check_in__lt=check_out,
                check_out__gt=check_in,
            )
            if exclude_reservation_id is not None:
                overlapping = overlapping.exclude(pk=exclude_reservation_id)
            if overlapping.exists():
                errors["lodge"] = "This lodge already has a request for those dates."

    if errors:
        raise ValidationError(errors)
