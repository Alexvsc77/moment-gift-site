"""Passwordless email and Telegram identity checks."""

import hashlib
import hmac
import os
import secrets
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.mail import send_mail
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone

from .models import EmailLoginCode, User


CODE_LIFETIME = timedelta(minutes=10)
EMAIL_WINDOW = timedelta(minutes=15)
IP_WINDOW = timedelta(hours=1)
MAX_EMAIL_REQUESTS = 3
MAX_IP_REQUESTS = 10
MAX_CODE_ATTEMPTS = 5
TELEGRAM_MAX_AGE_SECONDS = 600


class LoginRateLimited(Exception):
    """An email address or IP has requested too many codes."""


class LoginCodeInvalid(Exception):
    """The code is absent, expired, used, or incorrect."""


class TelegramLoginInvalid(Exception):
    """Telegram authorization data failed verification."""


def normalized_email(value):
    if not isinstance(value, str):
        raise ValidationError("Укажи корректный email.")
    email = value.strip().lower()
    validate_email(email)
    return email


def _digest(value):
    return hmac.new(settings.SECRET_KEY.encode(), value.encode(), hashlib.sha256).hexdigest()


def _code_digest(email, code):
    return _digest(f"email-code:{email}:{code}")


@transaction.atomic
def request_email_code(email, remote_ip):
    """Send a fresh code while enforcing limits shared across browser sessions."""

    email = normalized_email(email)
    if not isinstance(remote_ip, str) or not remote_ip:
        raise ValidationError("Не удалось определить источник запроса.")
    ip_digest = _digest(f"login-ip:{remote_ip}")
    now = timezone.now()

    if EmailLoginCode.objects.filter(email=email, created_at__gte=now - EMAIL_WINDOW).count() >= MAX_EMAIL_REQUESTS:
        raise LoginRateLimited("Слишком много кодов для этого адреса. Попробуй позже.")
    if EmailLoginCode.objects.filter(ip_digest=ip_digest, created_at__gte=now - IP_WINDOW).count() >= MAX_IP_REQUESTS:
        raise LoginRateLimited("Слишком много запросов. Попробуй позже.")
    if EmailLoginCode.objects.filter(email=email, created_at__gte=now - timedelta(minutes=1)).exists():
        raise LoginRateLimited("Новый код можно запросить через минуту.")

    code = f"{secrets.randbelow(1_000_000):06d}"
    EmailLoginCode.objects.filter(email=email, used_at__isnull=True).update(used_at=now)
    record = EmailLoginCode.objects.create(
        email=email,
        code_hash=_code_digest(email, code),
        ip_digest=ip_digest,
        expires_at=now + CODE_LIFETIME,
    )
    send_mail(
        subject="Код входа в Момент",
        message=f"Твой код входа: {code}\n\nОн действует 10 минут. Если ты не запрашивал код, просто проигнорируй письмо.",
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[email],
        fail_silently=False,
    )
    return record.id


def verify_email_code(email, code):
    """Consume one valid code and return its passwordless account."""

    email = normalized_email(email)
    if not isinstance(code, str) or len(code) != 6 or not code.isascii() or not code.isdigit():
        raise LoginCodeInvalid("Неверный или устаревший код.")

    invalid = False
    inactive = False
    user = None
    with transaction.atomic():
        now = timezone.now()
        record = (
            EmailLoginCode.objects.select_for_update()
            .filter(email=email, used_at__isnull=True, expires_at__gt=now)
            .order_by("-created_at")
            .first()
        )
        if record is None or record.attempts >= MAX_CODE_ATTEMPTS:
            invalid = True
        elif not hmac.compare_digest(record.code_hash, _code_digest(email, code)):
            record.attempts += 1
            if record.attempts >= MAX_CODE_ATTEMPTS:
                record.used_at = now
            record.save(update_fields=["attempts", "used_at"])
            invalid = True
        else:
            record.used_at = now
            record.save(update_fields=["used_at"])
            user = User.objects.filter(email__iexact=email).first()
            if user is None:
                user = User.objects.create_user(email=email)
            inactive = not user.is_active

    if invalid:
        raise LoginCodeInvalid("Неверный или устаревший код.")
    if inactive:
        raise LoginCodeInvalid("Вход в этот аккаунт недоступен.")
    return user


def verify_telegram_data(data):
    """Verify Telegram Login Widget data according to Telegram's HMAC rule."""

    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        raise ImproperlyConfigured("TELEGRAM_BOT_TOKEN is required for Telegram login.")
    if not isinstance(data, dict):
        raise TelegramLoginInvalid("Некорректные данные Telegram.")
    allowed = {"id", "first_name", "last_name", "username", "photo_url", "auth_date", "hash"}
    if not set(data).issubset(allowed) or not {"id", "auth_date", "hash"}.issubset(data):
        raise TelegramLoginInvalid("Некорректные данные Telegram.")
    if any(not isinstance(value, str) for value in data.values()):
        raise TelegramLoginInvalid("Некорректные данные Telegram.")

    try:
        telegram_id = str(int(data["id"]))
        auth_date = int(data["auth_date"])
    except (TypeError, ValueError) as exc:
        raise TelegramLoginInvalid("Некорректные данные Telegram.") from exc
    if int(telegram_id) <= 0:
        raise TelegramLoginInvalid("Некорректный ID Telegram.")
    age = int(timezone.now().timestamp()) - auth_date
    if age < -30 or age > TELEGRAM_MAX_AGE_SECONDS:
        raise TelegramLoginInvalid("Вход Telegram устарел. Попробуй ещё раз.")

    check_string = "\n".join(f"{key}={data[key]}" for key in sorted(data) if key != "hash")
    secret_key = hashlib.sha256(bot_token.encode()).digest()
    expected_hash = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_hash, data["hash"]):
        raise TelegramLoginInvalid("Подпись Telegram не совпадает.")

    display_name = " ".join(part for part in (data.get("first_name", ""), data.get("last_name", "")) if part).strip()[:100]
    with transaction.atomic():
        user = User.objects.filter(telegram_id=telegram_id).first()
        if user is None:
            user = User.objects.create_user(telegram_id=telegram_id, display_name=display_name)
        elif display_name and user.display_name != display_name:
            user.display_name = display_name
            user.save(update_fields=["display_name"])
        if not user.is_active:
            raise TelegramLoginInvalid("Вход в этот аккаунт недоступен.")
    return user
