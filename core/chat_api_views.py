import secrets
from datetime import timedelta

from django.contrib.auth.hashers import check_password, make_password
from django.core import signing
from django.db import transaction
from django.db.models import Count, OuterRef, Q, Subquery
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.authentication import TokenAuthentication
from rest_framework.exceptions import NotAuthenticated
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from .chat_serializers import (
    ChatCodeRequestSerializer,
    ChatCodeVerifySerializer,
    ChatMessageCreateSerializer,
    ChatMessageSerializer,
)
from .email_notifications import (
    send_chat_admin_notification,
    send_chat_verification_code,
)
from .models import ChatConversation, ChatEmailVerification, ChatMessage
from .staff_api_views import StaffApiPermission

CHAT_TOKEN_SALT = "core.customer-chat"
CHAT_TOKEN_MAX_AGE = 60 * 60 * 24 * 30


class ChatCodeRequest(APIView):
    authentication_classes = ()
    permission_classes = (AllowAny,)
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = "chat_code_request"

    def post(self, request):
        serializer = ChatCodeRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        name = serializer.validated_data["name"].strip()
        email = serializer.validated_data["email"].strip().casefold()
        now = timezone.now()

        ChatEmailVerification.objects.filter(
            created_at__lt=now - timedelta(days=1)
        ).delete()
        requests_this_hour = ChatEmailVerification.objects.filter(
            email__iexact=email,
            created_at__gte=now - timedelta(hours=1),
        ).count()
        if requests_this_hour >= 3:
            return Response(
                {"detail": "Too many code requests. Please try again later."},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        ChatEmailVerification.objects.filter(
            email__iexact=email,
            consumed_at__isnull=True,
        ).update(consumed_at=now)
        code = f"{secrets.randbelow(1_000_000):06d}"
        verification = ChatEmailVerification.objects.create(
            name=name,
            email=email,
            code_hash=make_password(code),
            expires_at=now + timedelta(minutes=10),
        )
        if not send_chat_verification_code(email, code):
            verification.consumed_at = now
            verification.save(update_fields=["consumed_at"])
            return Response(
                {"detail": "Could not send a verification code. Please try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response(
            {
                "detail": (
                    "A verification code has been sent. It expires in 10 minutes."
                )
            }
        )


class ChatCodeVerify(APIView):
    authentication_classes = ()
    permission_classes = (AllowAny,)
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = "chat_code_verify"

    @transaction.atomic
    def post(self, request):
        serializer = ChatCodeVerifySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"].strip().casefold()
        code = serializer.validated_data["code"]
        now = timezone.now()
        verification = (
            ChatEmailVerification.objects.select_for_update()
            .filter(email__iexact=email, consumed_at__isnull=True)
            .first()
        )
        if verification is None or verification.expires_at <= now:
            return Response(
                {"detail": "The verification code is invalid or expired."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if verification.attempts >= 5:
            return Response(
                {"detail": "Too many incorrect codes. Request a new one."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not check_password(code, verification.code_hash):
            verification.attempts += 1
            if verification.attempts >= 5:
                verification.consumed_at = now
            verification.save(update_fields=["attempts", "consumed_at"])
            return Response(
                {"detail": "The verification code is invalid or expired."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        verification.consumed_at = now
        verification.save(update_fields=["consumed_at"])
        conversation, created = ChatConversation.objects.get_or_create(
            email=email,
            defaults={"guest_name": verification.name},
        )
        if not created and conversation.guest_name != verification.name:
            conversation.guest_name = verification.name
            conversation.save(update_fields=["guest_name"])
        chat_token = signing.dumps(
            {"conversation_id": conversation.pk},
            salt=CHAT_TOKEN_SALT,
            compress=True,
        )
        messages = ChatMessage.objects.filter(conversation=conversation)
        return Response(
            {
                "token": chat_token,
                "conversation": {
                    "guest_name": conversation.guest_name,
                    "email": conversation.email,
                },
                "messages": ChatMessageSerializer(messages, many=True).data,
            }
        )


class CustomerChatAPIView(APIView):
    authentication_classes = ()
    permission_classes = (AllowAny,)

    def get_authenticate_header(self, request):
        return "Chat"

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        scheme, separator, token = request.headers.get(
            "Authorization", ""
        ).partition(" ")
        if not separator or scheme.lower() != "chat" or not token.strip():
            raise NotAuthenticated("Verify your email address to access this chat.")
        try:
            payload = signing.loads(
                token.strip(),
                salt=CHAT_TOKEN_SALT,
                max_age=CHAT_TOKEN_MAX_AGE,
            )
        except signing.BadSignature as error:
            raise NotAuthenticated("Your chat session expired. Verify your email again.") from error
        conversation_id = payload.get("conversation_id")
        if not isinstance(conversation_id, int):
            raise NotAuthenticated("Your chat session is invalid.")
        conversation = ChatConversation.objects.filter(pk=conversation_id).first()
        if conversation is None:
            raise NotAuthenticated("Your chat session is invalid.")
        self.conversation = conversation


class CustomerChatMessages(CustomerChatAPIView):
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = "chat_messages"

    def get(self, request):
        now = timezone.now()
        ChatMessage.objects.filter(
            conversation=self.conversation,
            sender=ChatMessage.Sender.STAFF,
            customer_read_at__isnull=True,
        ).update(customer_read_at=now)
        messages = ChatMessage.objects.filter(conversation=self.conversation)
        return Response({"messages": ChatMessageSerializer(messages, many=True).data})

    def post(self, request):
        serializer = ChatMessageCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        message = ChatMessage.objects.create(
            conversation=self.conversation,
            sender=ChatMessage.Sender.CUSTOMER,
            body=serializer.validated_data["body"],
        )
        ChatConversation.objects.filter(pk=self.conversation.pk).update(
            updated_at=message.created_at
        )
        send_chat_admin_notification(self.conversation, message)
        return Response(ChatMessageSerializer(message).data, status=status.HTTP_201_CREATED)


class StaffChatConversations(APIView):
    authentication_classes = (TokenAuthentication,)
    permission_classes = (StaffApiPermission,)

    def get(self, request):
        latest_message = ChatMessage.objects.filter(
            conversation_id=OuterRef("pk")
        ).order_by("-created_at", "-pk")
        conversations = (
            ChatConversation.objects.annotate(
                unread_count=Count(
                    "messages",
                    filter=Q(
                        messages__sender=ChatMessage.Sender.CUSTOMER,
                        messages__staff_read_at__isnull=True,
                    ),
                ),
                last_message_body=Subquery(latest_message.values("body")[:1]),
                last_message_sender=Subquery(latest_message.values("sender")[:1]),
                last_message_at=Subquery(latest_message.values("created_at")[:1]),
            )
            .order_by("-updated_at", "-pk")[:100]
        )
        return Response(
            [
                {
                    "id": conversation.pk,
                    "guest_name": conversation.guest_name,
                    "email": conversation.email,
                    "updated_at": conversation.updated_at,
                    "unread_count": conversation.unread_count,
                    "last_message_body": conversation.last_message_body or "",
                    "last_message_sender": conversation.last_message_sender or "",
                    "last_message_at": conversation.last_message_at,
                }
                for conversation in conversations
            ]
        )

    def post(self, request, pk):
        conversation = get_object_or_404(ChatConversation, pk=pk)
        serializer = ChatMessageCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        message = ChatMessage.objects.create(
            conversation=conversation,
            sender=ChatMessage.Sender.STAFF,
            staff_user=request.user,
            body=serializer.validated_data["body"],
        )
        ChatConversation.objects.filter(pk=conversation.pk).update(
            updated_at=message.created_at
        )
        return Response(ChatMessageSerializer(message).data, status=status.HTTP_201_CREATED)


class StaffChatUnreadCount(APIView):
    authentication_classes = (TokenAuthentication,)
    permission_classes = (StaffApiPermission,)

    def get(self, request):
        return Response(
            {
                "count": ChatMessage.objects.filter(
                    sender=ChatMessage.Sender.CUSTOMER,
                    staff_read_at__isnull=True,
                ).values("conversation_id").distinct().count()
            }
        )


class StaffChatMessages(APIView):
    authentication_classes = (TokenAuthentication,)
    permission_classes = (StaffApiPermission,)

    def get(self, request, pk):
        conversation = get_object_or_404(ChatConversation, pk=pk)
        ChatMessage.objects.filter(
            conversation=conversation,
            sender=ChatMessage.Sender.CUSTOMER,
            staff_read_at__isnull=True,
        ).update(staff_read_at=timezone.now())
        messages = ChatMessage.objects.filter(conversation=conversation)
        return Response(
            {
                "conversation": {
                    "id": conversation.pk,
                    "guest_name": conversation.guest_name,
                    "email": conversation.email,
                },
                "messages": ChatMessageSerializer(messages, many=True).data,
            }
        )
