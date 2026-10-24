"""HTTP endpoints for sign-in, drafts, and recipient interactions."""

import json
import logging

from django.contrib.auth import login, logout
from django.core.exceptions import ImproperlyConfigured, PermissionDenied, ValidationError
from django.db import IntegrityError
from django.http import FileResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .auth_service import (
    LoginCodeInvalid,
    LoginRateLimited,
    TelegramLoginInvalid,
    request_email_code,
    verify_email_code,
    verify_telegram_data,
)
from .media_service import remove_media, upload_media
from .models import Gift, GiftMedia, Payment, RecipientResponse, Tariff
from .notification_service import NotificationDeliveryError, send_open_notification
from .payment_service import PaymentProviderError, begin_payment, process_notification, request_refund
from .services import (
    GiftLinkUnavailable,
    delete_gift,
    duplicate_gift,
    get_public_gift,
    mark_gift_opened,
    public_link_path,
    record_response,
    release_due_links,
    save_draft,
)
from .vk_service import VKLoginError, complete_vk_login, start_vk_login


logger = logging.getLogger(__name__)


def _body(request):
    if len(request.body) > 1024 * 1024:
        raise ValidationError("Запрос слишком большой. Загружай медиафайлы отдельно.")
    try:
        data = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("Нужен корректный JSON.") from exc
    if not isinstance(data, dict):
        raise ValidationError("Нужен объект JSON.")
    return data


def _error(message, status=400, fields=None):
    data = {"error": message}
    if fields:
        data["fields"] = fields
    return JsonResponse(data, status=status)


def _validation_error(exc):
    if hasattr(exc, "message_dict"):
        return _error("Проверь заполненные поля.", fields=exc.message_dict)
    return _error(" ".join(exc.messages))


def _auth_required(request):
    if not request.user.is_authenticated:
        return _error("Войди в аккаунт.", status=401)
    return None


def _gift_for_owner(request, gift_id):
    try:
        return Gift.objects.select_related("tariff").get(
            pk=gift_id,
            owner=request.user,
            deleted_at__isnull=True,
        )
    except Gift.DoesNotExist:
        return None


def _owner_gift_data(request, gift):
    link = None
    try:
        link = request.build_absolute_uri(public_link_path(gift))
    except GiftLinkUnavailable:
        pass
    answer = RecipientResponse.objects.filter(gift_id=gift.pk).values("choice", "responded_at").first()
    return {
        "id": str(gift.pk),
        "status": gift.status,
        "tariff": gift.tariff_id,
        "template": gift.template,
        "music_track": gift.music_track,
        "title": gift.title,
        "message": gift.message,
        "recipient": gift.recipient_name,
        "sender": gift.sender_name,
        "occasion": gift.occasion,
        "contact_email": gift.contact_email,
        "custom_slug": gift.custom_slug,
        "scheduled_release_at": gift.scheduled_release_at.isoformat() if gift.scheduled_release_at else None,
        "link_available_at": gift.link_available_at.isoformat() if gift.link_available_at else None,
        "expires_at": gift.expires_at.isoformat() if gift.expires_at else None,
        "opened_at": gift.opened_at.isoformat() if gift.opened_at else None,
        "answered_at": gift.answered_at.isoformat() if gift.answered_at else None,
        "link": link,
        "answer": {
            "choice": answer["choice"],
            "responded_at": answer["responded_at"].isoformat(),
        } if answer else None,
        "media": [
            {
                "id": str(media.pk),
                "kind": media.kind,
                "url": request.build_absolute_uri(f"/api/gifts/{gift.pk}/media/{media.pk}/"),
            }
            for media in gift.media.all()
        ],
    }


def _send_media(media):
    content_type = "image/jpeg" if media.kind == GiftMedia.Kind.PHOTO else "video/mp4"
    try:
        response = FileResponse(media.file.open("rb"), content_type=content_type)
    except FileNotFoundError:
        return _error("Файл не найден.", status=404)
    response["Cache-Control"] = "private, no-store"
    return response


@require_POST
def email_code_request(request):
    try:
        data = _body(request)
        request_email_code(data.get("email"), request.META.get("REMOTE_ADDR", ""))
    except LoginRateLimited as exc:
        return _error(str(exc), status=429)
    except ValidationError as exc:
        return _validation_error(exc)
    except Exception:
        return _error("Не удалось отправить код. Попробуй позже.", status=503)
    return JsonResponse({"message": "Если адрес указан верно, код отправлен."})


@require_POST
def email_code_verify(request):
    try:
        data = _body(request)
        user = verify_email_code(data.get("email"), data.get("code"))
    except LoginCodeInvalid as exc:
        return _error(str(exc))
    except ValidationError as exc:
        return _validation_error(exc)
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    return JsonResponse({"id": str(user.pk), "email": user.email, "display_name": user.display_name})


@require_POST
def telegram_login(request):
    try:
        data = _body(request)
        user = verify_telegram_data(data)
    except TelegramLoginInvalid as exc:
        return _error(str(exc))
    except ValidationError as exc:
        return _validation_error(exc)
    except ImproperlyConfigured:
        return _error("Вход через Telegram пока не настроен.", status=503)
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    return JsonResponse({"id": str(user.pk), "email": user.email, "display_name": user.display_name})


@require_GET
def vk_login_start(request):
    try:
        url = start_vk_login(request)
    except ImproperlyConfigured:
        return _error("Вход через VK ID пока не настроен.", status=503)
    response = HttpResponseRedirect(url)
    response["Cache-Control"] = "no-store"
    response["Referrer-Policy"] = "no-referrer"
    return response


@require_GET
def vk_login_callback(request):
    try:
        user = complete_vk_login(request)
    except (VKLoginError, ImproperlyConfigured):
        response = HttpResponseRedirect("/?auth_error=vk#builder")
    else:
        login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        response = HttpResponseRedirect("/?auth=vk#builder")
    response["Cache-Control"] = "no-store"
    response["Referrer-Policy"] = "no-referrer"
    return response


@require_POST
def logout_view(request):
    logout(request)
    return JsonResponse({"ok": True})


@require_GET
def account(request):
    missing = _auth_required(request)
    if missing:
        return missing
    return JsonResponse({
        "id": str(request.user.pk),
        "email": request.user.email,
        "display_name": request.user.display_name,
    })


@require_GET
def payment_history(request):
    missing = _auth_required(request)
    if missing:
        return missing
    items = Payment.objects.filter(user=request.user).select_related("gift", "tariff")
    return JsonResponse({"payments": [
        {
            "id": str(payment.pk),
            "gift_id": str(payment.gift_id),
            "gift_title": payment.gift.title,
            "tariff": payment.tariff.name,
            "amount": str(payment.amount),
            "currency": payment.currency,
            "status": payment.status,
            "created_at": payment.created_at.isoformat(),
            "paid_at": payment.paid_at.isoformat() if payment.paid_at else None,
            "refund_requested_at": payment.refund_requested_at.isoformat() if payment.refund_requested_at else None,
            "provider_refund_id": payment.provider_refund_id,
            "refunded_at": payment.refunded_at.isoformat() if payment.refunded_at else None,
        }
        for payment in items
    ]})


@require_GET
def tariffs(request):
    items = [
        {
            "slug": tariff.slug,
            "name": tariff.name,
            "price": str(tariff.price),
            "currency": "RUB",
            "limits": tariff.limits_json,
        }
        for tariff in Tariff.objects.filter(is_active=True)
    ]
    return JsonResponse({"tariffs": items})


@require_http_methods(["GET", "POST"])
def gifts(request):
    missing = _auth_required(request)
    if missing:
        return missing
    if request.method == "GET":
        release_due_links()
        pending_notices = Gift.objects.filter(
            owner=request.user,
            opened_at__isnull=False,
            open_notification_sent_at__isnull=True,
            deleted_at__isnull=True,
            tariff__limits_json__notify_on_open=True,
        ).values_list("pk", flat=True)[:5]
        for pending_gift_id in pending_notices:
            try:
                send_open_notification(pending_gift_id)
            except NotificationDeliveryError:
                logger.warning("Could not send opening notification for gift %s", pending_gift_id)
        items = Gift.objects.filter(owner=request.user, deleted_at__isnull=True).select_related("tariff").prefetch_related("media")
        return JsonResponse({"gifts": [_owner_gift_data(request, gift) for gift in items]})
    try:
        gift = save_draft(request.user, _body(request))
    except ValidationError as exc:
        return _validation_error(exc)
    except IntegrityError:
        return _error("Такое название ссылки уже занято.", status=409)
    return JsonResponse({"gift": _owner_gift_data(request, gift)}, status=201)


@require_http_methods(["GET", "PUT", "DELETE"])
def gift_detail(request, gift_id):
    missing = _auth_required(request)
    if missing:
        return missing
    gift = _gift_for_owner(request, gift_id)
    if gift is None:
        return _error("Подарок не найден.", status=404)
    if request.method == "DELETE":
        try:
            delete_gift(request.user, gift.pk)
        except ValidationError as exc:
            return _validation_error(exc)
        return JsonResponse({"ok": True})
    if request.method == "PUT":
        try:
            gift = save_draft(request.user, _body(request), gift=gift)
        except ValidationError as exc:
            return _validation_error(exc)
        except PermissionDenied:
            return _error("Нет доступа к подарку.", status=403)
        except IntegrityError:
            return _error("Такое название ссылки уже занято.", status=409)
    return JsonResponse({"gift": _owner_gift_data(request, gift)})


@require_POST
def gift_duplicate(request, gift_id):
    missing = _auth_required(request)
    if missing:
        return missing
    if _gift_for_owner(request, gift_id) is None:
        return _error("Подарок не найден.", status=404)
    gift = duplicate_gift(request.user, gift_id)
    return JsonResponse({"gift": _owner_gift_data(request, gift)}, status=201)


@require_POST
def gift_checkout(request, gift_id):
    missing = _auth_required(request)
    if missing:
        return missing
    gift = _gift_for_owner(request, gift_id)
    if gift is None:
        return _error("Подарок не найден.", status=404)
    try:
        data = _body(request)
        receipt_email = data.get("receipt_email") or gift.contact_email
        if not isinstance(receipt_email, str):
            raise ValidationError({"receipt_email": "Укажи корректный email для чека."})
        payment, confirmation_url = begin_payment(
            request.user,
            gift.pk,
            request.build_absolute_uri("/#my-gifts"),
            receipt_email=receipt_email,
        )
    except ValidationError as exc:
        return _validation_error(exc)
    except PermissionDenied:
        return _error("Нет доступа к подарку.", status=403)
    except ImproperlyConfigured:
        return _error("Оплата пока не настроена.", status=503)
    except PaymentProviderError:
        return _error("Платёжный сервис временно недоступен. Попробуй снова.", status=503)
    return JsonResponse({
        "payment_id": str(payment.pk),
        "status": payment.status,
        "confirmation_url": confirmation_url,
    })


@require_POST
def gift_refund(request, gift_id):
    missing = _auth_required(request)
    if missing:
        return missing
    if not Gift.objects.filter(pk=gift_id, owner=request.user).exists():
        return _error("Подарок не найден.", status=404)
    try:
        payment, refund_status = request_refund(request.user, gift_id)
    except ValidationError as exc:
        return _validation_error(exc)
    except ImproperlyConfigured:
        return _error("Возвраты пока не настроены.", status=503)
    except PaymentProviderError as exc:
        return _error(str(exc), status=503)
    return JsonResponse({
        "payment_id": str(payment.pk),
        "payment_status": payment.status,
        "refund_status": refund_status,
    })


@require_POST
def gift_media_upload(request, gift_id):
    missing = _auth_required(request)
    if missing:
        return missing
    if _gift_for_owner(request, gift_id) is None:
        return _error("Подарок не найден.", status=404)
    try:
        media = upload_media(
            request.user,
            gift_id,
            request.FILES.get("file"),
            request.POST.get("kind"),
        )
    except ValidationError as exc:
        return _validation_error(exc)
    return JsonResponse({
        "id": str(media.pk),
        "kind": media.kind,
        "url": request.build_absolute_uri(f"/api/gifts/{gift_id}/media/{media.pk}/"),
    }, status=201)


@require_http_methods(["GET", "DELETE"])
def gift_media_detail(request, gift_id, media_id):
    missing = _auth_required(request)
    if missing:
        return missing
    gift = _gift_for_owner(request, gift_id)
    if gift is None:
        return _error("Подарок не найден.", status=404)
    try:
        media = GiftMedia.objects.get(pk=media_id, gift=gift)
    except GiftMedia.DoesNotExist:
        return _error("Файл не найден.", status=404)
    if request.method == "GET":
        return _send_media(media)
    try:
        remove_media(request.user, gift_id, media_id)
    except ValidationError as exc:
        return _validation_error(exc)
    return JsonResponse({"ok": True})


@csrf_exempt
@require_POST
def yookassa_webhook(request):
    try:
        result = process_notification(_body(request))
    except ValidationError as exc:
        return _validation_error(exc)
    except PaymentProviderError:
        return _error("Не удалось проверить уведомление.", status=503)
    except ImproperlyConfigured:
        return _error("Оплата пока не настроена.", status=503)
    return JsonResponse({"status": result})


@ensure_csrf_cookie
@require_GET
def public_gift(request, slug, token):
    try:
        get_public_gift(slug, token)
    except GiftLinkUnavailable:
        return _error("Ссылка недоступна или срок её действия истёк.", status=404)
    response = render(request, "gift.html", {
        "open_url": reverse("public-open", kwargs={"slug": slug, "token": token}),
        "answer_url": reverse("public-answer", kwargs={"slug": slug, "token": token}),
    })
    response["Cache-Control"] = "private, no-store"
    response["Referrer-Policy"] = "no-referrer"
    response["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    return response


@require_POST
def public_open(request, slug, token):
    try:
        gift, first_open = mark_gift_opened(slug, token)
    except GiftLinkUnavailable:
        return _error("Ссылка недоступна или срок её действия истёк.", status=404)
    if first_open and gift.tariff.limits_json.get("notify_on_open", False):
        try:
            send_open_notification(gift.pk)
        except NotificationDeliveryError:
            logger.warning("Could not send opening notification for gift %s", gift.pk)
    return JsonResponse({
        "first_open": first_open,
        "title": gift.title,
        "message": gift.message,
        "recipient": gift.recipient_name,
        "sender": gift.sender_name,
        "occasion": gift.occasion,
        "template": gift.template,
        "music_track": gift.music_track,
        "allow_answers": bool(gift.tariff.limits_json.get("allow_answers", False)),
        "service_signature": bool(gift.tariff.limits_json.get("service_signature", False)),
        "media": [
            {
                "kind": media.kind,
                "url": request.build_absolute_uri(
                    f"{public_link_path(gift)}media/{media.pk}/"
                ),
            }
            for media in gift.media.all()
        ],
    })


@require_GET
def public_media(request, slug, token, media_id):
    try:
        gift = get_public_gift(slug, token)
    except GiftLinkUnavailable:
        return _error("Ссылка недоступна или срок её действия истёк.", status=404)
    if gift.opened_at is None:
        return _error("Сначала открой подарок.", status=403)
    try:
        media = GiftMedia.objects.get(pk=media_id, gift=gift)
    except GiftMedia.DoesNotExist:
        return _error("Файл не найден.", status=404)
    return _send_media(media)


@require_POST
def public_answer(request, slug, token):
    try:
        response = record_response(slug, token, _body(request).get("choice"))
    except GiftLinkUnavailable:
        return _error("Ссылка недоступна или срок её действия истёк.", status=404)
    except ValidationError as exc:
        return _validation_error(exc)
    return JsonResponse({"choice": response.choice, "responded_at": response.responded_at.isoformat()}, status=201)
