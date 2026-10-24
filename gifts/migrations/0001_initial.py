"""Initial database schema for accounts, gifts, and payments."""

import uuid

import django.db.models.deletion
import django.db.models.functions.text
import django.utils.timezone
import gifts.models
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [
        migrations.CreateModel(
            name="Tariff",
            fields=[
                ("slug", models.SlugField(max_length=40, primary_key=True, serialize=False)),
                ("name", models.CharField(max_length=80)),
                ("price", models.DecimalField(decimal_places=2, max_digits=8)),
                ("limits_json", models.JSONField(default=dict)),
                ("is_active", models.BooleanField(default=True)),
                ("sort_order", models.PositiveSmallIntegerField(default=0)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["sort_order", "price"]},
        ),
        migrations.CreateModel(
            name="User",
            fields=[
                ("password", models.CharField(max_length=128, verbose_name="password")),
                ("last_login", models.DateTimeField(blank=True, null=True, verbose_name="last login")),
                ("is_superuser", models.BooleanField(default=False, help_text="Designates that this user has all permissions without explicitly assigning them.", verbose_name="superuser status")),
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("email", models.EmailField(blank=True, max_length=254, null=True, unique=True)),
                ("telegram_id", models.CharField(blank=True, max_length=32, null=True, unique=True)),
                ("vk_id", models.CharField(blank=True, max_length=64, null=True, unique=True)),
                ("display_name", models.CharField(blank=True, max_length=100)),
                ("is_staff", models.BooleanField(default=False)),
                ("is_active", models.BooleanField(default=True)),
                ("date_joined", models.DateTimeField(default=django.utils.timezone.now)),
                ("groups", models.ManyToManyField(blank=True, help_text="The groups this user belongs to. A user will get all permissions granted to each of their groups.", related_name="user_set", related_query_name="user", to="auth.group", verbose_name="groups")),
                ("user_permissions", models.ManyToManyField(blank=True, help_text="Specific permissions for this user.", related_name="user_set", related_query_name="user", to="auth.permission", verbose_name="user permissions")),
            ],
            managers=[("objects", gifts.models.UserManager())],
        ),
        migrations.CreateModel(
            name="Gift",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("status", models.CharField(choices=[("draft", "Черновик"), ("awaiting_payment", "Ожидает оплаты"), ("awaiting_link", "Ожидает выдачи ссылки"), ("link_ready", "Ссылка готова"), ("opened", "Открыт"), ("answered", "Получен ответ"), ("expired", "Срок ссылки истёк"), ("cancelled", "Отменён"), ("refunded", "Возврат")], default="draft", max_length=24)),
                ("recipient_name", models.CharField(blank=True, max_length=50)),
                ("title", models.CharField(max_length=70)),
                ("message", models.TextField(max_length=600)),
                ("sender_name", models.CharField(blank=True, max_length=50)),
                ("occasion", models.CharField(blank=True, max_length=50)),
                ("template", models.CharField(choices=[("petal", "Нежность"), ("night", "Вечер"), ("sun", "Солнце")], default="petal", max_length=20)),
                ("custom_slug", models.SlugField(allow_unicode=True, blank=True, max_length=60, null=True, unique=True)),
                ("public_token", models.CharField(blank=True, editable=False, max_length=64, null=True, unique=True)),
                ("scheduled_release_at", models.DateTimeField(blank=True, null=True)),
                ("link_available_at", models.DateTimeField(blank=True, null=True)),
                ("expires_at", models.DateTimeField(blank=True, null=True)),
                ("opened_at", models.DateTimeField(blank=True, null=True)),
                ("answered_at", models.DateTimeField(blank=True, null=True)),
                ("deleted_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("owner", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="gifts", to=settings.AUTH_USER_MODEL)),
                ("tariff", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, to="gifts.tariff")),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="GiftMedia",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("file", models.FileField(upload_to=gifts.models.gift_media_path)),
                ("kind", models.CharField(choices=[("photo", "Фото"), ("video", "Видео")], max_length=10)),
                ("size_bytes", models.PositiveBigIntegerField()),
                ("sort_order", models.PositiveSmallIntegerField(default=0)),
                ("uploaded_at", models.DateTimeField(auto_now_add=True)),
                ("gift", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="media", to="gifts.gift")),
            ],
            options={"ordering": ["sort_order", "uploaded_at"]},
        ),
        migrations.CreateModel(
            name="RecipientResponse",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("choice", models.CharField(choices=[("yes", "Да"), ("think", "Подумаю")], max_length=10)),
                ("responded_at", models.DateTimeField(auto_now_add=True)),
                ("gift", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="response", to="gifts.gift")),
            ],
        ),
        migrations.CreateModel(
            name="Payment",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("amount", models.DecimalField(decimal_places=2, max_digits=8)),
                ("currency", models.CharField(default="RUB", max_length=3)),
                ("status", models.CharField(choices=[("pending", "Ожидает оплаты"), ("succeeded", "Оплачен"), ("cancelled", "Отменён"), ("refunded", "Возвращён")], default="pending", max_length=16)),
                ("provider_payment_id", models.CharField(blank=True, max_length=100, null=True, unique=True)),
                ("idempotence_key", models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ("paid_at", models.DateTimeField(blank=True, null=True)),
                ("refunded_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("gift", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="payments", to="gifts.gift")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="payments", to=settings.AUTH_USER_MODEL)),
                ("tariff", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to="gifts.tariff")),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.AddConstraint(
            model_name="user",
            constraint=models.UniqueConstraint(django.db.models.functions.text.Lower("email"), condition=models.Q(("email__isnull", False)), name="unique_user_email_case_insensitive"),
        ),
        migrations.AddConstraint(
            model_name="payment",
            constraint=models.UniqueConstraint(condition=models.Q(("status__in", ["succeeded", "refunded"])), fields=("gift",), name="one_completed_payment_per_gift"),
        ),
        migrations.AddIndex(model_name="gift", index=models.Index(fields=["owner", "status"], name="gifts_gift_owner_i_492edc_idx")),
        migrations.AddIndex(model_name="gift", index=models.Index(fields=["scheduled_release_at"], name="gifts_gift_schedul_7cd58d_idx")),
        migrations.AddIndex(model_name="gift", index=models.Index(fields=["expires_at"], name="gifts_gift_expires_13ef1d_idx")),
    ]
