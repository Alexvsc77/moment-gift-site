"""Deliver and track the first-open notice included with Premium gifts."""

import json
import os
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone

from .models import Gift


class NotificationDeliveryError(Exception):
    """The notice was not delivered and can be retried later."""


def _send_telegram(chat_id, message):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise NotificationDeliveryError("Telegram-бот не настроен.")
    payload = json.dumps({"chat_id": chat_id, "text": message}).encode("utf-8")
    request = Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=8) as response:
            result = json.load(response)
    except Exception as exc:
        raise NotificationDeliveryError("Telegram не принял уведомление.") from exc
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise NotificationDeliveryError("Telegram не подтвердил доставку уведомления.")


def send_open_notification(gift_id):
    """Send once after the first open; a failed send leaves the gift retryable."""

    with transaction.atomic():
        gift = (
            Gift.objects.select_for_update()
            .select_related("owner", "tariff")
            .get(pk=gift_id)
        )
        if (
            gift.opened_at is None
            or gift.open_notification_sent_at is not None
            or gift.deleted_at is not None
            or not gift.tariff.limits_json.get("notify_on_open", False)
        ):
            return False

        message = (
            f"Твой подарок «{gift.title}» открыли. "
            "Ответ, если получатель его оставит, появится в разделе «Мои подарки»."
        )
        email = gift.contact_email or gift.owner.email
        if email:
            try:
                delivered = send_mail(
                    "Подарок открыт · Момент",
                    message,
                    settings.DEFAULT_FROM_EMAIL,
                    [email],
                    fail_silently=False,
                )
            except Exception as exc:
                raise NotificationDeliveryError("Почта не отправила уведомление.") from exc
            if delivered != 1:
                raise NotificationDeliveryError("Почта не подтвердила отправку уведомления.")
        elif gift.owner.telegram_id:
            _send_telegram(gift.owner.telegram_id, message)
        else:
            raise NotificationDeliveryError("У владельца нет адреса для уведомления.")

        gift.open_notification_sent_at = timezone.now()
        gift.save(update_fields=["open_notification_sent_at", "updated_at"])
        return True
