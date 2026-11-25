from rest_framework import generics, status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from .email_notifications import send_support_ticket_notification
from .staff_api_views import StaffApiMixin, StaffReservationPagination
from .models import SupportTicket
from .support_serializers import (
    SupportTicketCreateSerializer,
    SupportTicketSerializer,
)


class SupportTicketCreate(APIView):
    authentication_classes = ()
    permission_classes = (AllowAny,)
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = "support_ticket_create"

    def post(self, request):
        serializer = SupportTicketCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        ticket = serializer.save()
        ticket.email_sent = send_support_ticket_notification(ticket)
        if ticket.email_sent:
            ticket.save(update_fields=["email_sent"])
        return Response(
            {
                "ticket": SupportTicketSerializer(ticket).data,
                "email_sent": ticket.email_sent,
            },
            status=status.HTTP_201_CREATED,
        )


class StaffSupportTicketList(StaffApiMixin, generics.ListAPIView):
    queryset = SupportTicket.objects.all()
    serializer_class = SupportTicketSerializer
    pagination_class = StaffReservationPagination
