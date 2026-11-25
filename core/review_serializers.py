from rest_framework import serializers

from .models import CustomerReview


class CustomerReviewSerializer(serializers.ModelSerializer):
    class Meta:
        model = CustomerReview
        fields = ("id", "reviewer_name", "rating", "comment", "created_at")
        read_only_fields = fields


class ReviewAccessSubmitSerializer(serializers.Serializer):
    token = serializers.CharField(max_length=1024)
    rating = serializers.IntegerField(min_value=1, max_value=5)
    comment = serializers.CharField(max_length=2000, trim_whitespace=True)
