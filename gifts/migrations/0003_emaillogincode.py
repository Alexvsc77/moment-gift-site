"""Persist single-use login codes and their rate-limit history."""

import uuid

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("gifts", "0002_seed_tariffs")]

    operations = [
        migrations.CreateModel(
            name="EmailLoginCode",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("email", models.EmailField(db_index=True, max_length=254)),
                ("code_hash", models.CharField(max_length=128)),
                ("ip_digest", models.CharField(db_index=True, max_length=64)),
                ("expires_at", models.DateTimeField()),
                ("attempts", models.PositiveSmallIntegerField(default=0)),
                ("used_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "ordering": ["-created_at"],
                "indexes": [
                    models.Index(fields=["email", "created_at"], name="gifts_email_email_f8b434_idx"),
                    models.Index(fields=["ip_digest", "created_at"], name="gifts_email_ip_dige_5b9e81_idx"),
                ],
            },
        ),
    ]
