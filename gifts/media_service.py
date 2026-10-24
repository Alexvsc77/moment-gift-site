"""Validate and store private gift media for unpaid drafts."""

from io import BytesIO

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from PIL import Image, ImageOps, UnidentifiedImageError

from .models import Gift, GiftMedia


PHOTO_PIXELS_LIMIT = 20_000_000


def _draft_for_owner(owner, gift_id):
    if not owner or not owner.is_authenticated:
        raise PermissionDenied("Нужно войти в аккаунт.")
    try:
        gift = Gift.objects.select_for_update().select_related("tariff").get(pk=gift_id)
    except Gift.DoesNotExist as exc:
        raise ValidationError("Подарок не найден.") from exc
    if gift.owner_id != owner.pk or gift.deleted_at:
        raise PermissionDenied("Нет доступа к подарку.")
    if gift.status != Gift.Status.DRAFT:
        raise ValidationError("Медиа можно менять только до оплаты.")
    return gift


def _prepare_photo(uploaded, max_bytes):
    if uploaded.size > max_bytes or uploaded.size == 0:
        raise ValidationError(f"Фото должно быть не больше {max_bytes // (1024 * 1024)} МБ.")
    try:
        uploaded.seek(0)
        with Image.open(uploaded) as source:
            if source.format not in ("JPEG", "PNG", "WEBP"):
                raise ValidationError("Поддерживаются JPG, PNG и WebP.")
            if source.width * source.height > PHOTO_PIXELS_LIMIT:
                raise ValidationError("Слишком большое разрешение фото.")
            source.verify()
        uploaded.seek(0)
        with Image.open(uploaded) as source:
            photo = ImageOps.exif_transpose(source).convert("RGB")
            photo.thumbnail((1600, 1600))
            output = BytesIO()
            photo.save(output, format="JPEG", quality=82, optimize=True)
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ValidationError("Не удалось прочитать фото.") from exc
    return ContentFile(output.getvalue(), name="photo.jpg")


def _prepare_video(uploaded, max_bytes):
    if uploaded.size > max_bytes or uploaded.size == 0:
        raise ValidationError(f"Видео должно быть не больше {max_bytes // (1024 * 1024)} МБ.")
    uploaded.seek(0)
    header = uploaded.read(12)
    uploaded.seek(0)
    if len(header) < 12 or header[4:8] != b"ftyp":
        raise ValidationError("Загрузи видео в формате MP4.")
    uploaded.name = "video.mp4"
    return uploaded


@transaction.atomic
def upload_media(owner, gift_id, uploaded, kind):
    """Store a validated file within the chosen tariff's limits."""

    gift = _draft_for_owner(owner, gift_id)
    if uploaded is None:
        raise ValidationError("Выбери файл для загрузки.")
    limits = gift.tariff.limits_json if gift.tariff else {}
    if kind == GiftMedia.Kind.PHOTO:
        max_count = int(limits.get("max_photos", 0))
        if gift.media.filter(kind=GiftMedia.Kind.PHOTO).count() >= max_count:
            raise ValidationError(f"В этом тарифе можно загрузить не больше {max_count} фото.")
        prepared = _prepare_photo(uploaded, int(limits.get("max_photo_bytes", 0)))
        order = gift.media.filter(kind=GiftMedia.Kind.PHOTO).count()
    elif kind == GiftMedia.Kind.VIDEO:
        if not limits.get("allow_video"):
            raise ValidationError("Видео доступно только в Премиуме.")
        if gift.media.filter(kind=GiftMedia.Kind.VIDEO).exists():
            raise ValidationError("К подарку можно добавить одно видео.")
        prepared = _prepare_video(uploaded, int(limits.get("max_video_bytes", 0)))
        order = 0
    else:
        raise ValidationError("Неизвестный тип файла.")

    media = GiftMedia(gift=gift, kind=kind, size_bytes=prepared.size, sort_order=order)
    media.file.save(prepared.name, prepared, save=False)
    try:
        media.save()
    except Exception:
        media.file.delete(save=False)
        raise
    return media


@transaction.atomic
def remove_media(owner, gift_id, media_id):
    gift = _draft_for_owner(owner, gift_id)
    try:
        media = GiftMedia.objects.get(pk=media_id, gift=gift)
    except GiftMedia.DoesNotExist as exc:
        raise ValidationError("Файл не найден.") from exc
    storage = media.file.storage
    filename = media.file.name
    media.delete()
    transaction.on_commit(lambda: storage.delete(filename))
