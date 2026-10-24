"""Add a separate, unique idempotence key for each payment refund."""

import uuid

from django.db import migrations, models


def assign_refund_keys(apps, schema_editor):
    Payment = apps.get_model("gifts", "Payment")
    for payment in Payment.objects.using(schema_editor.connection.alias).all().iterator():
        payment.refund_idempotence_key = uuid.uuid4()
        payment.save(update_fields=["refund_idempotence_key"])


class Migration(migrations.Migration):
    dependencies = [("gifts", "0003_emaillogincode")]

    operations = [
        migrations.AddField(
            model_name="payment",
            name="provider_refund_id",
            field=models.CharField(blank=True, max_length=100, null=True, unique=True),
        ),
        migrations.AddField(
            model_name="payment",
            name="refund_requested_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="payment",
            name="refund_idempotence_key",
            field=models.UUIDField(editable=False, null=True),
        ),
        migrations.RunPython(assign_refund_keys, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="payment",
            name="refund_idempotence_key",
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
    ]
