import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Create the first staff superuser from Render environment variables."

    def handle(self, *args, **options):
        username = os.environ.get("DJANGO_SUPERUSER_USERNAME")
        password = os.environ.get("DJANGO_SUPERUSER_PASSWORD")
        email = os.environ.get("DJANGO_SUPERUSER_EMAIL", "")
        if not username or not password:
            self.stdout.write("Admin bootstrap skipped: username/password are not set.")
            return
        user_model = get_user_model()
        if user_model.objects.filter(username=username).exists():
            self.stdout.write(f"Admin account '{username}' already exists.")
            return
        user_model.objects.create_superuser(
            username=username, email=email, password=password
        )
        self.stdout.write(self.style.SUCCESS(f"Created staff admin '{username}'."))
