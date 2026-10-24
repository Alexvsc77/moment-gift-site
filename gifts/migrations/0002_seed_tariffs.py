"""Starting tariffs; prices and limits remain editable in the database."""

from decimal import Decimal

from django.db import migrations


PHOTO_LIMIT = 5 * 1024 * 1024
VIDEO_LIMIT = 50 * 1024 * 1024


def seed_tariffs(apps, schema_editor):
    Tariff = apps.get_model("gifts", "Tariff")
    tariffs = [
        {
            "slug": "lite",
            "name": "Лайт",
            "price": Decimal("200.00"),
            "sort_order": 1,
            "limits_json": {
                "max_photos": 3,
                "max_photo_bytes": PHOTO_LIMIT,
                "link_days": 7,
                "allowed_templates": ["petal"],
                "allow_music": False,
                "allow_answers": False,
                "allow_video": False,
                "allow_animated_templates": False,
                "allow_scheduled_release": False,
                "notify_on_open": False,
                "service_signature": True,
            },
        },
        {
            "slug": "standard",
            "name": "Стандарт",
            "price": Decimal("350.00"),
            "sort_order": 2,
            "limits_json": {
                "max_photos": 10,
                "max_photo_bytes": PHOTO_LIMIT,
                "link_days": 30,
                "allowed_templates": ["petal", "night", "sun"],
                "allow_music": True,
                "allow_answers": True,
                "allow_video": False,
                "allow_animated_templates": False,
                "allow_scheduled_release": False,
                "notify_on_open": False,
                "service_signature": False,
            },
        },
        {
            "slug": "premium",
            "name": "Премиум",
            "price": Decimal("450.00"),
            "sort_order": 3,
            "limits_json": {
                "max_photos": 10,
                "max_photo_bytes": PHOTO_LIMIT,
                "max_video_bytes": VIDEO_LIMIT,
                "link_days": 30,
                "allowed_templates": ["petal", "night", "sun"],
                "allow_music": True,
                "allow_answers": True,
                "allow_video": True,
                "allow_animated_templates": True,
                "allow_scheduled_release": True,
                "notify_on_open": True,
                "service_signature": False,
            },
        },
    ]

    for tariff in tariffs:
        slug = tariff.pop("slug")
        Tariff.objects.using(schema_editor.connection.alias).update_or_create(
            slug=slug,
            defaults={**tariff, "is_active": True},
        )


class Migration(migrations.Migration):
    dependencies = [("gifts", "0001_initial")]

    operations = [migrations.RunPython(seed_tariffs, migrations.RunPython.noop)]
