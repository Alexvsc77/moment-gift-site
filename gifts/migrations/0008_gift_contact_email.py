"""Keep a gift's receipt and opening-notification email separate from login."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("gifts", "0007_premium_template_limits")]

    operations = [
        migrations.AddField(
            model_name="gift",
            name="contact_email",
            field=models.EmailField(blank=True, default="", max_length=254),
        ),
    ]
