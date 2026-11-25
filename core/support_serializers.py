from rest_framework import serializers

from .models import SupportTicket


class SupportTicketCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=120, trim_whitespace=True)
    email = serializers.EmailField(max_length=254)
    phone = serializers.CharField(max_length=40, allow_blank=True, required=False)
    subject = serializers.CharField(max_length=200, trim_whitespace=True)
    message = serializers.CharField(max_length=5000, trim_whitespace=True)

    def create(self, validated_data):
        return SupportTicket.objects.create(**validated_data)


class SupportTicketSerializer(serializers.ModelSerializer):
    class Meta:
        model = SupportTicket
        fields = (
            "id",
            "name",
            "email",
            "phone",
            "subject",
            "message",
            "email_sent",
            "created_at",
        )
        read_only_fields = fields
