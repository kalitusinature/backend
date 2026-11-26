from django.core.management.base import BaseCommand, CommandError
from django.db import connections, transaction

from core.models import (
    ChatConversation,
    ChatEmailVerification,
    ChatMessage,
    CustomerReview,
    GuestCart,
    GuestCartItem,
    LodgeReservation,
    Order,
    OrderEmailVerification,
    OrderItem,
    Payment,
    PaymentReceipt,
    SupportTicket,
)


DATASETS = (
    ("customer reviews", CustomerReview),
    ("payment receipts", PaymentReceipt),
    ("payments", Payment),
    ("order items", OrderItem),
    ("orders", Order),
    ("reservations", LodgeReservation),
    ("guest cart items", GuestCartItem),
    ("guest carts", GuestCart),
    ("order email verifications", OrderEmailVerification),
    ("chat messages", ChatMessage),
    ("chat conversations", ChatConversation),
    ("chat email verifications", ChatEmailVerification),
    ("support tickets", SupportTicket),
)


class Command(BaseCommand):
    help = (
        "Clear transactional/test activity while preserving staff, catalog, "
        "lodge, and availability setup."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--database",
            default="default",
            help="Database alias to inspect and clear (default: default).",
        )
        parser.add_argument(
            "--confirm",
            action="store_true",
            help="Perform deletion. Without this flag, only show the dry-run counts.",
        )
        parser.add_argument(
            "--expected-database",
            help="Required with --confirm; must exactly match the target database name.",
        )
        parser.add_argument(
            "--expected-host",
            help="Required with --confirm when the target database has a configured host.",
        )

    def handle(self, *args, **options):
        database_alias = options["database"]
        if database_alias not in connections:
            raise CommandError(f"Unknown database alias: {database_alias}")

        connection = connections[database_alias]
        database_name = str(connection.settings_dict["NAME"])
        database_host = connection.settings_dict.get("HOST") or ""
        self.stdout.write(
            "Target database: "
            f"alias={database_alias}, engine={connection.vendor}, "
            f"name={database_name}, host={database_host or '(not configured)'}"
        )
        self.stdout.write(
            self.style.WARNING(
                "This removes all records in the listed activity tables, not only "
                "records identifiable as test data."
            )
        )

        counts = []
        for label, model in DATASETS:
            count = model.objects.using(database_alias).count()
            counts.append((label, model))
            self.stdout.write(f"{label}: {count}")

        if not options["confirm"]:
            self.stdout.write(
                self.style.WARNING(
                    "Dry run only. Review the target and counts; no records were deleted."
                )
            )
            return

        if options["expected_database"] != database_name:
            raise CommandError(
                "Refusing to delete: --expected-database must exactly match "
                "the displayed target database name."
            )
        expected_host = options["expected_host"]
        if database_host and expected_host != database_host:
            raise CommandError(
                "Refusing to delete: --expected-host must exactly match "
                "the displayed target database host."
            )
        if not database_host and expected_host:
            raise CommandError(
                "Refusing to delete: this database has no configured host, "
                "so --expected-host must not be provided."
            )

        with transaction.atomic(using=database_alias):
            for label, model in counts:
                deleted_count, _ = (
                    model.objects.using(database_alias).all().delete()
                )
                self.stdout.write(f"Deleted {label}: {deleted_count}")

        self.stdout.write(
            self.style.SUCCESS(
                "Transactional activity cleared. Staff accounts, catalog, lodges, "
                "blockouts, and other setup data were preserved."
            )
        )
