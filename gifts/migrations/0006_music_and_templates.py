"""Store a music choice and register the Premium visual templates."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("gifts", "0005_gift_open_notification")]

    operations = [
        migrations.AddField(
            model_name="gift",
            name="music_track",
            field=models.CharField(
                blank=True,
                choices=[
                    ("calm", "Тихий вечер"),
                    ("warm", "Тёплый свет"),
                    ("bright", "Радостный день"),
                ],
                default="",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="gift",
            name="template",
            field=models.CharField(
                choices=[
                    ("petal", "Нежность"),
                    ("night", "Вечер"),
                    ("sun", "Солнце"),
                    ("aurora", "Северное сияние"),
                    ("bloom", "Цветение"),
                ],
                default="petal",
                max_length=20,
            ),
        ),
    ]
