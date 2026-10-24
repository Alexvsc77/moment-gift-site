"""Track successful first-open notifications for premium gifts."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("gifts", "0004_payment_refund")]

    operations = [
        migrations.AddField(
            model_name="gift",
            name="open_notification_sent_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
