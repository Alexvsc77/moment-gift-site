"""YooKassa requests and verified payment notifications."""

import base64
import json
import os
import uuid
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, PermissionDenied, ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone

from .models import Gift, Payment
from .services import confirm_payment, validate_ready_for_payment


API_BASE = "https://api.yookassa.ru/v3"


class PaymentProviderError(Exception):
    """The payment provider did not return a usable response."""


def _credentials():
    shop_id = os.getenv("YOOKASSA_SHOP_ID")
    secret_key = os.getenv("YOOKASSA_SECRET_KEY")
    if not shop_id or not secret_key:
        raise ImproperlyConfigured("YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY are required.")
    return shop_id, secret_key


def _api_request(method, path, payload=None, idempotence_key=None):
    shop_id, secret_key = _credentials()
    authorization = base64.b64encode(f"{shop_id}:{secret_key}".encode()).decode("ascii")
    headers = {
        "Authorization": f"Basic {authorization}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if idempotence_key:
        headers["Idempotence-Key"] = str(idempotence_key)
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(f"{API_BASE}{path}", data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=12) as response:
            result = json.load(response)
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        raise PaymentProviderError("Не удалось связаться с платёжным сервисом.") from exc
    if not isinstance(result, dict):
        raise PaymentProviderError("Платёжный сервис вернул неожиданный ответ.")
    return result


def _receipt(payment, receipt_email):
    if os.getenv("YOOKASSA_SEND_RECEIPT", "0") != "1":
        return None
    email = (receipt_email or payment.user.email or "").strip().lower()
    validate_email(email)
    vat_code = os.getenv("YOOKASSA_VAT_CODE")
    if not vat_code or not vat_code.isdigit():
        raise ImproperlyConfigured("YOOKASSA_VAT_CODE is required when receipts are enabled.")
    return {
        "customer": {"email": email},
        "items": [{
            "description": f"Создание подарка, тариф {payment.tariff.name}",
            "quantity": "1.00",
            "amount": {"value": f"{payment.amount:.2f}", "currency": "RUB"},
            "vat_code": int(vat_code),
            "payment_subject": "service",
            "payment_mode": "full_payment",
        }],
    }


def _payment_from_provider(provider_id):
    if not isinstance(provider_id, str) or not provider_id or len(provider_id) > 100:
        raise PaymentProviderError("Некорректный идентификатор платежа.")
    return _api_request("GET", f"/payments/{quote(provider_id, safe='')}")


def _refund_from_provider(refund_id):
    if not isinstance(refund_id, str) or not refund_id or len(refund_id) > 100:
        raise PaymentProviderError("Некорректный идентификатор возврата.")
    return _api_request("GET", f"/refunds/{quote(refund_id, safe='')}")


def _verify_refund(refund, payment, expected_id=None):
    if not isinstance(refund, dict) or not isinstance(refund.get("id"), str):
        raise PaymentProviderError("ЮKassa не вернула ID возврата.")
    if expected_id and refund["id"] != expected_id:
        raise PaymentProviderError("ID возврата не совпадает.")
    if refund.get("payment_id") != payment.provider_payment_id:
        raise PaymentProviderError("Возврат относится к другому платежу.")
    amount = refund.get("amount") or {}
    try:
        value = Decimal(str(amount.get("value")))
    except (InvalidOperation, TypeError) as exc:
        raise PaymentProviderError("Неизвестная сумма возврата.") from exc
    if amount.get("currency") != payment.currency or value != payment.amount:
        raise PaymentProviderError("Сумма или валюта возврата не совпадает.")
    if payment.provider_refund_id and payment.provider_refund_id != refund["id"]:
        raise PaymentProviderError("ID возврата в системе не совпадает.")
    return refund["id"]


def _complete_refund(payment_id, refund):
    with transaction.atomic():
        payment = Payment.objects.select_for_update().get(pk=payment_id)
        gift = Gift.objects.select_for_update().get(pk=payment.gift_id)
        refund_id = _verify_refund(refund, payment)
        if gift.opened_at is not None:
            raise PaymentProviderError("Подарок уже открыт; возврат требует ручной проверки.")
        if payment.status == Payment.Status.REFUNDED:
            return payment
        if payment.status != Payment.Status.SUCCEEDED:
            raise PaymentProviderError("Этот платёж нельзя вернуть.")
        payment.provider_refund_id = refund_id
        payment.status = Payment.Status.REFUNDED
        payment.refunded_at = timezone.now()
        payment.save(update_fields=["provider_refund_id", "status", "refunded_at", "updated_at"])
        gift.status = Gift.Status.REFUNDED
        gift.public_token = None
        gift.save(update_fields=["status", "public_token", "updated_at"])
        return payment


def _restore_canceled_refund(payment_id):
    with transaction.atomic():
        payment = Payment.objects.select_for_update().get(pk=payment_id)
        gift = Gift.objects.select_for_update().get(pk=payment.gift_id)
        if payment.status != Payment.Status.SUCCEEDED:
            return
        payment.provider_refund_id = None
        payment.refund_requested_at = None
        payment.refund_idempotence_key = uuid.uuid4()
        payment.save(update_fields=[
            "provider_refund_id", "refund_requested_at", "refund_idempotence_key", "updated_at"
        ])
        if gift.status == Gift.Status.CANCELLED and gift.opened_at is None:
            if gift.link_available_at is None:
                gift.status = Gift.Status.AWAITING_LINK
            elif gift.expires_at and gift.expires_at <= timezone.now():
                gift.status = Gift.Status.EXPIRED
            else:
                gift.status = Gift.Status.LINK_READY
            gift.save(update_fields=["status", "updated_at"])


def begin_payment(owner, gift_id, return_url, receipt_email=None):
    """Create one local attempt, then request or recover its provider redirect."""

    if not owner or not owner.is_authenticated:
        raise PermissionDenied("Нужно войти в аккаунт.")
    _credentials()
    parsed_url = urlparse(return_url)
    if parsed_url.scheme != "https" and not (settings.DEBUG and parsed_url.scheme == "http"):
        raise ValidationError("Некорректный адрес возврата после оплаты.")
    if not parsed_url.netloc:
        raise ValidationError("Некорректный адрес возврата после оплаты.")

    with transaction.atomic():
        gift = Gift.objects.select_for_update().select_related("tariff").get(pk=gift_id)
        if gift.owner_id != owner.pk or gift.deleted_at:
            raise PermissionDenied("Нет доступа к подарку.")
        if gift.status == Gift.Status.DRAFT:
            validate_ready_for_payment(gift)
            payment = Payment.objects.create(
                user=owner,
                gift=gift,
                tariff=gift.tariff,
                amount=gift.tariff.price,
            )
            gift.status = Gift.Status.AWAITING_PAYMENT
            gift.save(update_fields=["status", "updated_at"])
        elif gift.status == Gift.Status.AWAITING_PAYMENT:
            payment = (
                Payment.objects.select_for_update()
                .filter(gift=gift, status=Payment.Status.PENDING)
                .order_by("-created_at")
                .first()
            )
            if payment is None:
                raise ValidationError("Не удалось найти платёж. Обнови страницу и попробуй снова.")
        else:
            raise ValidationError("Этот подарок уже оплачен или недоступен для оплаты.")

    payment = Payment.objects.select_related("user", "tariff", "gift").get(pk=payment.pk)
    if payment.provider_payment_id:
        provider_payment = _payment_from_provider(payment.provider_payment_id)
    else:
        payload = {
            "amount": {"value": f"{payment.amount:.2f}", "currency": payment.currency},
            "capture": True,
            "confirmation": {"type": "redirect", "return_url": return_url},
            "description": f"Создание подарка {str(payment.gift_id)[:8]}",
            "metadata": {"payment_id": str(payment.pk), "gift_id": str(payment.gift_id)},
        }
        receipt = _receipt(payment, receipt_email)
        if receipt:
            payload["receipt"] = receipt
        provider_payment = _api_request(
            "POST", "/payments", payload=payload, idempotence_key=payment.idempotence_key
        )

    provider_id = provider_payment.get("id")
    if not isinstance(provider_id, str) or not provider_id:
        raise PaymentProviderError("Платёжный сервис не вернул ID платежа.")
    with transaction.atomic():
        locked = Payment.objects.select_for_update().get(pk=payment.pk)
        if locked.provider_payment_id and locked.provider_payment_id != provider_id:
            raise PaymentProviderError("Идентификатор платежа изменился.")
        if not locked.provider_payment_id:
            locked.provider_payment_id = provider_id
            locked.save(update_fields=["provider_payment_id", "updated_at"])

    confirmation = provider_payment.get("confirmation") or {}
    confirmation_url = confirmation.get("confirmation_url")
    if provider_payment.get("status") == "pending":
        parsed_confirmation = urlparse(confirmation_url or "")
        if parsed_confirmation.scheme != "https" or not parsed_confirmation.netloc:
            raise PaymentProviderError("Платёжный сервис не вернул безопасную ссылку оплаты.")
    else:
        confirmation_url = None
    return payment, confirmation_url


def request_refund(owner, gift_id):
    """Request a full, idempotent refund before the gift has been opened."""

    if not owner or not owner.is_authenticated:
        raise PermissionDenied("Нужно войти в аккаунт.")
    _credentials()
    with transaction.atomic():
        gift = Gift.objects.select_for_update().get(pk=gift_id, owner=owner)
        if gift.opened_at is not None:
            raise ValidationError("После открытия подарка возврат недоступен.")
        payment = (
            Payment.objects.select_for_update()
            .filter(gift=gift, user=owner, status__in=[Payment.Status.SUCCEEDED, Payment.Status.REFUNDED])
            .order_by("-created_at")
            .first()
        )
        if payment is None:
            raise ValidationError("Подтверждённый платёж не найден.")
        if payment.status == Payment.Status.REFUNDED:
            return payment, "succeeded"
        if not payment.provider_payment_id:
            raise PaymentProviderError("У платежа нет ID ЮKassa.")
        if payment.refund_requested_at is None:
            payment.refund_requested_at = timezone.now()
            payment.save(update_fields=["refund_requested_at", "updated_at"])
        if gift.status != Gift.Status.CANCELLED:
            gift.status = Gift.Status.CANCELLED
            gift.save(update_fields=["status", "updated_at"])

    payment = Payment.objects.get(pk=payment.pk)
    if payment.provider_refund_id:
        refund = _refund_from_provider(payment.provider_refund_id)
    else:
        refund = _api_request(
            "POST", "/refunds",
            payload={
                "payment_id": payment.provider_payment_id,
                "amount": {"value": f"{payment.amount:.2f}", "currency": payment.currency},
            },
            idempotence_key=payment.refund_idempotence_key,
        )
    refund_id = _verify_refund(refund, payment)
    refund = _refund_from_provider(refund_id)
    _verify_refund(refund, payment, refund_id)

    with transaction.atomic():
        locked = Payment.objects.select_for_update().get(pk=payment.pk)
        if locked.provider_refund_id and locked.provider_refund_id != refund_id:
            raise PaymentProviderError("ID возврата в системе не совпадает.")
        if not locked.provider_refund_id:
            locked.provider_refund_id = refund_id
            locked.save(update_fields=["provider_refund_id", "updated_at"])

    status = refund.get("status")
    if status == "succeeded":
        return _complete_refund(payment.pk, refund), status
    if status == "canceled":
        _restore_canceled_refund(payment.pk)
        raise PaymentProviderError("ЮKassa отклонила возврат. Попробуй позже или обратись в поддержку.")
    if status == "pending":
        return Payment.objects.get(pk=payment.pk), status
    raise PaymentProviderError("Неизвестный статус возврата.")


def process_notification(payload):
    """Never trust the webhook body without a fresh provider API lookup."""

    if not isinstance(payload, dict) or payload.get("type") != "notification":
        raise PaymentProviderError("Некорректное уведомление.")
    event = payload.get("event")
    if event not in ("payment.succeeded", "payment.canceled", "refund.succeeded"):
        return "ignored"
    object_data = payload.get("object")
    if not isinstance(object_data, dict):
        raise PaymentProviderError("В уведомлении нет данных платежа.")
    if event == "refund.succeeded":
        refund_id = object_data.get("id")
        refund = _refund_from_provider(refund_id)
        if refund.get("id") != refund_id or refund.get("status") != "succeeded":
            raise PaymentProviderError("Статус возврата не совпадает с уведомлением.")
        try:
            payment = Payment.objects.get(provider_payment_id=refund.get("payment_id"))
        except Payment.DoesNotExist as exc:
            raise PaymentProviderError("Платёж возврата не найден.") from exc
        _verify_refund(refund, payment, refund_id)
        _complete_refund(payment.pk, refund)
        return "refunded"
    provider_id = object_data.get("id")
    provider_payment = _payment_from_provider(provider_id)
    if provider_payment.get("id") != provider_id:
        raise PaymentProviderError("Не совпадает ID платежа.")
    metadata = provider_payment.get("metadata")
    if not isinstance(metadata, dict):
        raise PaymentProviderError("В платеже нет данных для сверки.")
    payment_id = metadata.get("payment_id")
    gift_id = metadata.get("gift_id")
    try:
        payment = Payment.objects.select_related("gift", "tariff").get(pk=payment_id)
    except (Payment.DoesNotExist, ValueError, TypeError) as exc:
        raise PaymentProviderError("Платёж не найден в системе.") from exc
    if str(payment.gift_id) != gift_id:
        raise PaymentProviderError("Платёж относится к другому подарку.")
    if payment.provider_payment_id and payment.provider_payment_id != provider_id:
        raise PaymentProviderError("Не совпадает ID платежа в системе.")
    amount = provider_payment.get("amount") or {}
    try:
        confirmed_amount = Decimal(str(amount.get("value")))
    except (InvalidOperation, TypeError) as exc:
        raise PaymentProviderError("Неизвестная сумма платежа.") from exc
    if amount.get("currency") != "RUB" or confirmed_amount != payment.amount:
        raise PaymentProviderError("Сумма или валюта платежа не совпадает.")

    provider_status = provider_payment.get("status")
    if event == "payment.succeeded" and provider_status == "succeeded":
        confirm_payment(payment.pk, provider_id, confirmed_amount)
        return "confirmed"
    if event == "payment.canceled" and provider_status == "canceled":
        with transaction.atomic():
            payment = Payment.objects.select_for_update().get(pk=payment.pk)
            gift = Gift.objects.select_for_update().get(pk=payment.gift_id)
            if payment.status == Payment.Status.PENDING:
                payment.status = Payment.Status.CANCELLED
                payment.save(update_fields=["status", "updated_at"])
                if gift.status == Gift.Status.AWAITING_PAYMENT:
                    gift.status = Gift.Status.DRAFT
                    gift.save(update_fields=["status", "updated_at"])
        return "canceled"
    raise PaymentProviderError("Статус платежа у провайдера не совпадает с уведомлением.")
