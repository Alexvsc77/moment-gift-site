"""Server-side rules for editing, paying for, and opening gifts."""

import secrets
from datetime import datetime, timedelta
from decimal import Decimal
from urllib.parse import quote

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from .models import Gift, GiftMedia, Payment, RecipientResponse, Tariff


class GiftLinkUnavailable(Exception):
    """The public link is missing, not released yet, or expired."""


def _text(value, label, max_length, required=False):
    if not isinstance(value, str):
        raise ValidationError({label: "Нужно передать текст."})
    cleaned = value.strip()
    if required and not cleaned:
        raise ValidationError({label: "Заполни это поле."})
    if len(cleaned) > max_length:
        raise ValidationError({label: f"Максимум {max_length} символов."})
    return cleaned


def _contact_email(value):
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        raise ValidationError({"contact_email": "Укажи корректный email."})
    email = value.strip().lower()
    try:
        validate_email(email)
    except ValidationError as exc:
        raise ValidationError({"contact_email": "Укажи корректный email."}) from exc
    return email


def _tariff_from_payload(payload):
    slug = payload.get("plan", "standard")
    if not isinstance(slug, str):
        raise ValidationError({"plan": "Неизвестный тариф."})
    try:
        return Tariff.objects.get(slug=slug, is_active=True)
    except Tariff.DoesNotExist as exc:
        raise ValidationError({"plan": "Неизвестный тариф."}) from exc


def _clean_slug(value, gift=None):
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValidationError({"custom_slug": "Введи название ссылки."})
    slug = slugify(value.strip(), allow_unicode=True)
    if not slug or len(slug) > 60:
        raise ValidationError({"custom_slug": "Название ссылки должно содержать от 1 до 60 символов."})
    matches = Gift.objects.filter(custom_slug=slug)
    if gift is not None:
        matches = matches.exclude(pk=gift.pk)
    if matches.exists():
        raise ValidationError({"custom_slug": "Такое название ссылки уже занято."})
    return slug


def _schedule(value, tariff):
    if value in (None, ""):
        return None
    if not tariff.limits_json.get("allow_scheduled_release", False):
        raise ValidationError({"scheduled_release_at": "Отложенная выдача доступна только в Премиуме."})
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError({"scheduled_release_at": "Укажи корректную дату и время."}) from exc
    if not isinstance(value, datetime) or timezone.is_naive(value):
        raise ValidationError({"scheduled_release_at": "Укажи дату и время с часовым поясом."})
    if value <= timezone.now():
        raise ValidationError({"scheduled_release_at": "Выбери время в будущем."})
    return value


@transaction.atomic
def save_draft(owner, payload, gift=None):
    """Create or update only an unpaid draft owned by this account."""

    if not owner or not owner.is_authenticated:
        raise PermissionDenied("Нужно войти в аккаунт.")
    if not isinstance(payload, dict):
        raise ValidationError("Некорректный черновик.")
    if gift is not None:
        gift = Gift.objects.select_for_update().get(pk=gift.pk)
        if gift.owner_id != owner.pk:
            raise PermissionDenied("Это не твой подарок.")
        if gift.status != Gift.Status.DRAFT or gift.deleted_at:
            raise ValidationError("Этот подарок уже нельзя редактировать.")
    else:
        gift = Gift(owner=owner)

    tariff = _tariff_from_payload(payload)
    template = payload.get("template", Gift.Template.PETAL)
    if template not in tariff.limits_json.get("allowed_templates", []):
        raise ValidationError({"template": "Шаблон недоступен в выбранном тарифе."})
    music_track = payload.get("music_track", "") or ""
    if not isinstance(music_track, str) or music_track not in ("", *Gift.MusicTrack.values):
        raise ValidationError({"music_track": "Выбери мелодию из библиотеки."})
    if music_track and not tariff.limits_json.get("allow_music", False):
        raise ValidationError({"music_track": "Музыка недоступна в выбранном тарифе."})

    gift.tariff = tariff
    gift.template = template
    gift.music_track = music_track
    gift.title = _text(payload.get("title", ""), "title", 70)
    gift.message = _text(payload.get("message", ""), "message", 600)
    gift.recipient_name = _text(payload.get("recipient", ""), "recipient", 50)
    gift.sender_name = _text(payload.get("sender", ""), "sender", 50)
    gift.occasion = _text(payload.get("occasion", ""), "occasion", 50)
    gift.contact_email = _contact_email(payload.get("contact_email", ""))
    gift.custom_slug = _clean_slug(payload.get("custom_slug"), gift)
    gift.scheduled_release_at = _schedule(payload.get("scheduled_release_at"), tariff)
    gift.save()
    return gift


def duplicate_gift(owner, gift_id):
    """Create an unpaid copy with independent media files and no public link."""

    if not owner or not owner.is_authenticated:
        raise PermissionDenied("Нужно войти в аккаунт.")
    copied_files = []
    try:
        with transaction.atomic():
            source = Gift.objects.select_for_update().get(
                pk=gift_id, owner=owner, deleted_at__isnull=True
            )
            copy = Gift.objects.create(
                owner=owner,
                tariff=source.tariff,
                template=source.template,
                music_track=source.music_track,
                title=source.title,
                message=source.message,
                recipient_name=source.recipient_name,
                sender_name=source.sender_name,
                occasion=source.occasion,
                contact_email=source.contact_email,
            )
            for original in source.media.all():
                media = GiftMedia(
                    gift=copy, kind=original.kind,
                    size_bytes=original.size_bytes, sort_order=original.sort_order,
                )
                with original.file.open("rb") as file:
                    media.file.save(original.file.name, file, save=False)
                copied_files.append((media.file.storage, media.file.name))
                media.save()
        return copy
    except Exception:
        for storage, filename in copied_files:
            storage.delete(filename)
        raise


@transaction.atomic
def delete_gift(owner, gift_id):
    """Hide a gift and revoke its public link while retaining payment records."""

    if not owner or not owner.is_authenticated:
        raise PermissionDenied("Нужно войти в аккаунт.")
    gift = Gift.objects.select_for_update().get(
        pk=gift_id, owner=owner, deleted_at__isnull=True
    )
    if gift.status == Gift.Status.AWAITING_PAYMENT:
        raise ValidationError("Сначала дождись завершения платежа.")
    if gift.opened_at is None and Payment.objects.filter(
        gift=gift, status=Payment.Status.SUCCEEDED
    ).exists():
        raise ValidationError("Перед удалением ещё не открытого подарка оформи возврат.")
    gift.deleted_at = timezone.now()
    gift.save(update_fields=["deleted_at", "updated_at"])
    return gift


def validate_ready_for_payment(gift):
    """Reject incomplete drafts before a payment request is created."""

    if gift.status != Gift.Status.DRAFT or gift.deleted_at:
        raise ValidationError("Этот подарок уже нельзя отправить на оплату.")
    if not gift.tariff or not gift.tariff.is_active:
        raise ValidationError("Выбери действующий тариф.")
    _text(gift.title, "title", 70, required=True)
    _text(gift.message, "message", 600, required=True)
    if not gift.custom_slug:
        raise ValidationError({"custom_slug": "Придумай название ссылки."})
    _contact_email(gift.contact_email)
    if (
        gift.tariff.limits_json.get("notify_on_open", False)
        and not (gift.contact_email or gift.owner.email or gift.owner.telegram_id)
    ):
        raise ValidationError({"contact_email": "Укажи email для уведомления об открытии подарка."})
    if gift.template not in gift.tariff.limits_json.get("allowed_templates", []):
        raise ValidationError({"template": "Шаблон недоступен в выбранном тарифе."})
    if gift.music_track not in ("", *Gift.MusicTrack.values):
        raise ValidationError({"music_track": "Выбери мелодию из библиотеки."})
    if gift.music_track and not gift.tariff.limits_json.get("allow_music", False):
        raise ValidationError({"music_track": "Музыка недоступна в выбранном тарифе."})
    max_photos = gift.tariff.limits_json.get("max_photos", 0)
    if gift.media.filter(kind="photo").count() > max_photos:
        raise ValidationError({"photos": f"В тарифе можно использовать не больше {max_photos} фото."})
    if gift.media.filter(kind="video").exists() and not gift.tariff.limits_json.get("allow_video", False):
        raise ValidationError({"video": "Видео недоступно в выбранном тарифе."})
    max_video_bytes = int(gift.tariff.limits_json.get("max_video_bytes", 0))
    if gift.media.filter(kind="video", size_bytes__gt=max_video_bytes).exists():
        raise ValidationError({"video": "Видео превышает лимит выбранного тарифа."})


def _make_link_ready(gift, now):
    gift.public_token = secrets.token_urlsafe(32)
    gift.link_available_at = now
    gift.expires_at = now + timedelta(days=int(gift.tariff.limits_json["link_days"]))
    gift.status = Gift.Status.LINK_READY


@transaction.atomic
def confirm_payment(payment_id, provider_payment_id, confirmed_amount):
    """Called only after a server-side provider status check says succeeded."""

    payment = Payment.objects.select_for_update().select_related("tariff").get(pk=payment_id)
    gift = Gift.objects.select_for_update().get(pk=payment.gift_id)
    if payment.user_id != gift.owner_id or payment.tariff_id != gift.tariff_id:
        raise ValidationError("Платёж не соответствует подарку.")
    if Decimal(str(confirmed_amount)) != payment.amount:
        raise ValidationError("Сумма подтверждённого платежа не совпадает.")
    if payment.status == Payment.Status.SUCCEEDED:
        if payment.provider_payment_id != provider_payment_id:
            raise ValidationError("Идентификатор платежа не совпадает.")
        return gift
    if payment.status != Payment.Status.PENDING or gift.status != Gift.Status.AWAITING_PAYMENT:
        raise ValidationError("Этот платёж нельзя подтвердить.")
    if not provider_payment_id or (payment.provider_payment_id and payment.provider_payment_id != provider_payment_id):
        raise ValidationError("Идентификатор платежа не совпадает.")

    now = timezone.now()
    payment.provider_payment_id = provider_payment_id
    payment.status = Payment.Status.SUCCEEDED
    payment.paid_at = now
    payment.save(update_fields=["provider_payment_id", "status", "paid_at", "updated_at"])

    if gift.scheduled_release_at and gift.scheduled_release_at > now:
        gift.status = Gift.Status.AWAITING_LINK
    else:
        _make_link_ready(gift, now)
    gift.save(update_fields=["public_token", "link_available_at", "expires_at", "status", "updated_at"])
    return gift


def release_due_links(now=None):
    """Call from a scheduled server task; never expose links before their time."""

    now = now or timezone.now()
    released = 0
    ids = Gift.objects.filter(
        status=Gift.Status.AWAITING_LINK,
        scheduled_release_at__lte=now,
        payments__status=Payment.Status.SUCCEEDED,
    ).values_list("pk", flat=True).distinct()
    for gift_id in ids:
        with transaction.atomic():
            gift = Gift.objects.select_for_update().select_related("tariff").get(pk=gift_id)
            if gift.status != Gift.Status.AWAITING_LINK or gift.scheduled_release_at > now:
                continue
            _make_link_ready(gift, now)
            gift.save(update_fields=["public_token", "link_available_at", "expires_at", "status", "updated_at"])
            released += 1
    return released


def public_link_path(gift):
    if (
        not gift.public_token
        or not gift.custom_slug
        or not gift.link_available_at
        or gift.status not in (Gift.Status.LINK_READY, Gift.Status.OPENED, Gift.Status.ANSWERED)
        or not gift.expires_at
        or gift.expires_at <= timezone.now()
    ):
        raise GiftLinkUnavailable("Ссылка ещё не готова.")
    return f"/g/{quote(gift.custom_slug)}/{gift.public_token}/"


def _public_gift(slug, token, lock=False):
    queryset = Gift.objects.select_related("tariff")
    if lock:
        queryset = queryset.select_for_update()
    try:
        gift = queryset.get(custom_slug=slug, deleted_at__isnull=True)
    except Gift.DoesNotExist as exc:
        raise GiftLinkUnavailable("Ссылка не найдена.") from exc
    if not gift.public_token or not secrets.compare_digest(gift.public_token, token):
        raise GiftLinkUnavailable("Ссылка не найдена.")
    if gift.status not in (Gift.Status.LINK_READY, Gift.Status.OPENED, Gift.Status.ANSWERED):
        raise GiftLinkUnavailable("Ссылка недоступна.")
    if not gift.expires_at or gift.expires_at <= timezone.now():
        raise GiftLinkUnavailable("Срок ссылки истёк.")
    return gift


def get_public_gift(slug, token):
    """Viewing the unopened page does not count as opening the gift."""

    return _public_gift(slug, token)


@transaction.atomic
def mark_gift_opened(slug, token):
    """Call only after the recipient explicitly presses the open button."""

    gift = _public_gift(slug, token, lock=True)
    first_open = gift.opened_at is None
    if first_open:
        gift.opened_at = timezone.now()
        gift.status = Gift.Status.OPENED
        gift.save(update_fields=["opened_at", "status", "updated_at"])
    return gift, first_open


@transaction.atomic
def record_response(slug, token, choice):
    gift = _public_gift(slug, token, lock=True)
    if not gift.tariff.limits_json.get("allow_answers", False):
        raise ValidationError("Ответ недоступен в этом тарифе.")
    if gift.opened_at is None:
        raise ValidationError("Сначала открой подарок.")
    if choice not in RecipientResponse.Choice.values:
        raise ValidationError("Неизвестный вариант ответа.")
    if RecipientResponse.objects.filter(gift=gift).exists():
        raise ValidationError("Ответ уже отправлен.")

    response = RecipientResponse.objects.create(gift=gift, choice=choice)
    gift.answered_at = response.responded_at
    gift.status = Gift.Status.ANSWERED
    gift.save(update_fields=["answered_at", "status", "updated_at"])
    return response
