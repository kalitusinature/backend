# Generated manually for multi-image lodge support
from django.db import migrations, models
import django.db.models.deletion


def forwards(apps, schema_editor):
    Lodge = apps.get_model('core', 'Lodge')
    LodgeImage = apps.get_model('core', 'LodgeImage')
    for lodge in Lodge.objects.all():
        if lodge.image_url:
            LodgeImage.objects.create(
                lodge=lodge,
                image_url=lodge.image_url,
                image_public_id=getattr(lodge, 'image_public_id', '') or '',
                sort_order=0,
            )


def backwards(apps, schema_editor):
    Lodge = apps.get_model('core', 'Lodge')
    LodgeImage = apps.get_model('core', 'LodgeImage')
    for lodge in Lodge.objects.all():
        first = LodgeImage.objects.filter(lodge=lodge).order_by('sort_order', 'id').first()
        if first:
            lodge.image_url = first.image_url
            lodge.image_public_id = first.image_public_id
            lodge.save(update_fields=['image_url', 'image_public_id'])
        else:
            lodge.image_url = ''
            lodge.image_public_id = ''
            lodge.save(update_fields=['image_url', 'image_public_id'])


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0010_guest_cart'),
    ]

    operations = [
        migrations.CreateModel(
            name='LodgeImage',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('image_url', models.URLField(max_length=1000)),
                ('image_public_id', models.CharField(blank=True, max_length=255)),
                ('sort_order', models.PositiveSmallIntegerField(default=0)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('lodge', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='images', to='core.lodge')),
            ],
            options={
                'ordering': ['sort_order', 'id'],
            },
        ),
        migrations.RunPython(forwards, backwards),
    ]
