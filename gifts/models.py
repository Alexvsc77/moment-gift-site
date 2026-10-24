"""Accounts, gifts, tariff configuration, and payment records."""

import uuid

from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import PermissionsMixin
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower
from django.utils import timezone


class UserManager(BaseUserManager):
    use_in_migrations = True

    def create_user(self, email=None, telegram_id=None, vk_id=None, **extra_fields):
        if not any((email, telegram_id, vk_id)):
            raise ValueError("An email, Telegram ID, or VK ID is required.")

        normalized_email = email.strip().lower() if email else None
        user = self.model(
            email=normalized_email,
            telegram_id=telegram_id,
            vk_id=vk_id,
            **extra_fields,
        )
        user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_superuser(self, email, **extra_fields):
        if not email:
            raise ValueError("A superuser needs an email address.")
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        if extra_fields["is_staff"] is not True or extra_fields["is_superuser"] is not True:
            raise ValueError("A superuser must have staff and superuser privileges.")
        return self.create_user(email=email, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin):
    """A passwordless account that can have one or several login methods."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(unique=True, null=True, blank=True)
    telegram_id = models.CharField(max_length=32, unique=True, null=True, blank=True)
    vk_id = models.CharField(max_length=64, unique=True, null=True, blank=True)
    display_name = models.CharField(max_length=100, blank=True)
    is_staff = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    date_joined = models.DateTimeField(default=timezone.now)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []
    objects = UserManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                Lower("email"),
                condition=Q(email__isnull=False),
                name="unique_user_email_case_insensitive",
            )
        ]

    def __str__(self):
        return self.email or self.display_name or str(self.pk)


class EmailLoginCode(models.Model):
    """Short-lived, single-use email codes with no plaintext code in storage."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(db_index=True)
    code_hash = models.CharField(max_length=128)
    ip_digest = models.CharField(max_length=64, db_index=True)
    expires_at = models.DateTimeField()
    attempts = models.PositiveSmallIntegerField(default=0)
    used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["email", "created_at"]),
            models.Index(fields=["ip_digest", "created_at"]),
        ]

    def __str__(self):
        return f"Login code for {self.email}"


class Tariff(models.Model):
    """Prices and feature limits are data, so they can change without a release."""

    slug = models.SlugField(max_length=40, primary_key=True)
    name = models.CharField(max_length=80)
    price = models.DecimalField(max_digits=8, decimal_places=2)
    limits_json = models.JSONField(default=dict)
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort_order", "price"]

    def __str__(self):
        return f"{self.name} — {self.price} ₽"


class Gift(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Черновик"
        AWAITING_PAYMENT = "awaiting_payment", "Ожидает оплаты"
        AWAITING_LINK = "awaiting_link", "Ожидает выдачи ссылки"
        LINK_READY = "link_ready", "Ссылка готова"
        OPENED = "opened", "Открыт"
        ANSWERED = "answered", "Получен ответ"
        EXPIRED = "expired", "Срок ссылки истёк"
        CANCELLED = "cancelled", "Отменён"
        REFUNDED = "refunded", "Возврат"

    class Template(models.TextChoices):
        PETAL = "petal", "Нежность"
        NIGHT = "night", "Вечер"
        SUN = "sun", "Солнце"
        AURORA = "aurora", "Северное сияние"
        BLOOM = "bloom", "Цветение"

    class MusicTrack(models.TextChoices):
        CALM = "calm", "Тихий вечер"
        WARM = "warm", "Тёплый свет"
        BRIGHT = "bright", "Радостный день"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name="gifts")
    tariff = models.ForeignKey(Tariff, on_delete=models.PROTECT, null=True, blank=True)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.DRAFT)

    recipient_name = models.CharField(max_length=50, blank=True)
    title = models.CharField(max_length=70)
    message = models.TextField(max_length=600)
    sender_name = models.CharField(max_length=50, blank=True)
    occasion = models.CharField(max_length=50, blank=True)
    contact_email = models.EmailField(blank=True, default="")
    template = models.CharField(max_length=20, choices=Template.choices, default=Template.PETAL)
    music_track = models.CharField(max_length=20, choices=MusicTrack.choices, blank=True, default="")

    custom_slug = models.SlugField(max_length=60, allow_unicode=True, unique=True, null=True, blank=True)
    public_token = models.CharField(max_length=64, unique=True, null=True, blank=True, editable=False)
    scheduled_release_at = models.DateTimeField(null=True, blank=True)
    link_available_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    opened_at = models.DateTimeField(null=True, blank=True)
    open_notification_sent_at = models.DateTimeField(null=True, blank=True)
    answered_at = models.DateTimeField(null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["owner", "status"]),
            models.Index(fields=["scheduled_release_at"]),
            models.Index(fields=["expires_at"]),
        ]

    def __str__(self):
        return self.title or str(self.pk)


def gift_media_path(instance, filename):
    """Keep uploaded media under the owning gift's random ID."""
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"
    return f"gifts/{instance.gift_id}/{uuid.uuid4().hex}.{extension}"


class GiftMedia(models.Model):
    class Kind(models.TextChoices):
        PHOTO = "photo", "Фото"
        VIDEO = "video", "Видео"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    gift = models.ForeignKey(Gift, on_delete=models.CASCADE, related_name="media")
    file = models.FileField(upload_to=gift_media_path)
    kind = models.CharField(max_length=10, choices=Kind.choices)
    size_bytes = models.PositiveBigIntegerField()
    sort_order = models.PositiveSmallIntegerField(default=0)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["sort_order", "uploaded_at"]


class Payment(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Ожидает оплаты"
        SUCCEEDED = "succeeded", "Оплачен"
        CANCELLED = "cancelled", "Отменён"
        REFUNDED = "refunded", "Возвращён"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="payments")
    gift = models.ForeignKey(Gift, on_delete=models.PROTECT, related_name="payments")
    tariff = models.ForeignKey(Tariff, on_delete=models.PROTECT)
    amount = models.DecimalField(max_digits=8, decimal_places=2)
    currency = models.CharField(max_length=3, default="RUB")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    provider_payment_id = models.CharField(max_length=100, unique=True, null=True, blank=True)
    idempotence_key = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    provider_refund_id = models.CharField(max_length=100, unique=True, null=True, blank=True)
    refund_idempotence_key = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    refund_requested_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    refunded_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["gift"],
                condition=Q(status__in=["succeeded", "refunded"]),
                name="one_completed_payment_per_gift",
            )
        ]

    def __str__(self):
        return f"{self.amount} {self.currency} for {self.gift_id} ({self.status})"


class RecipientResponse(models.Model):
    class Choice(models.TextChoices):
        YES = "yes", "Да"
        THINK = "think", "Подумаю"

    gift = models.OneToOneField(Gift, on_delete=models.CASCADE, related_name="response")
    choice = models.CharField(max_length=10, choices=Choice.choices)
    responded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.gift_id}: {self.get_choice_display()}"
