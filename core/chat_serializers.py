from rest_framework import serializers

from .models import ChatMessage


class ChatCodeRequestSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=120, trim_whitespace=True)
    email = serializers.EmailField(max_length=254)


class ChatCodeVerifySerializer(serializers.Serializer):
    email = serializers.EmailField(max_length=254)
    code = serializers.RegexField(regex=r"^\d{6}$", max_length=6)


class ChatMessageCreateSerializer(serializers.Serializer):
    body = serializers.CharField(max_length=2000, trim_whitespace=True)

    def validate_body(self, value):
        if not value:
            raise serializers.ValidationError("Message cannot be empty.")
        return value


class ChatMessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = ChatMessage
        fields = ("id", "sender", "body", "created_at")
