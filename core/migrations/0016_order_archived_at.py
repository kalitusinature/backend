from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0015_orderemailverification"),
    ]

    operations = [
        migrations.AddField(
            model_name="order",
            name="archived_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
    ]
