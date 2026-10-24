"""Make animated templates available only in Premium."""

from django.db import migrations


PREMIUM_TEMPLATES = ("aurora", "bloom")


def update_templates(apps, schema_editor, add):
    Tariff = apps.get_model("gifts", "Tariff")
    tariff = Tariff.objects.using(schema_editor.connection.alias).filter(slug="premium").first()
    if tariff is None:
        return
    limits = dict(tariff.limits_json or {})
    allowed = list(limits.get("allowed_templates") or [])
    if add:
        for template in PREMIUM_TEMPLATES:
            if template not in allowed:
                allowed.append(template)
    else:
        allowed = [template for template in allowed if template not in PREMIUM_TEMPLATES]
    limits["allowed_templates"] = allowed
    tariff.limits_json = limits
    tariff.save(update_fields=["limits_json"])


def forwards(apps, schema_editor):
    update_templates(apps, schema_editor, True)


def backwards(apps, schema_editor):
    update_templates(apps, schema_editor, False)


class Migration(migrations.Migration):
    dependencies = [("gifts", "0006_music_and_templates")]

    operations = [migrations.RunPython(forwards, backwards)]
