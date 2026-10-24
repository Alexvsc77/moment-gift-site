"""Public routes for the gift site."""

import os

from django.http import JsonResponse
from django.urls import path
from django.views.generic import TemplateView
from django.views.decorators.csrf import ensure_csrf_cookie

from gifts import views


def health_check(request):
    return JsonResponse({"status": "ok"})


urlpatterns = [
    path("", ensure_csrf_cookie(TemplateView.as_view(
        template_name="index.html",
        extra_context={
            "telegram_bot_username": os.getenv("TELEGRAM_BOT_USERNAME", ""),
            "vk_app_id": os.getenv("VK_ID_APP_ID", ""),
        },
    )), name="home"),
    path("health/", health_check, name="health"),
    path("api/tariffs/", views.tariffs, name="tariffs"),
    path("api/auth/email/request/", views.email_code_request, name="email-code-request"),
    path("api/auth/email/verify/", views.email_code_verify, name="email-code-verify"),
    path("api/auth/telegram/", views.telegram_login, name="telegram-login"),
    path("api/auth/vk/start/", views.vk_login_start, name="vk-login-start"),
    path("api/auth/vk/callback/", views.vk_login_callback, name="vk-login-callback"),
    path("api/auth/logout/", views.logout_view, name="logout"),
    path("api/account/", views.account, name="account"),
    path("api/account/payments/", views.payment_history, name="payment-history"),
    path("api/gifts/", views.gifts, name="gifts"),
    path("api/gifts/<uuid:gift_id>/", views.gift_detail, name="gift-detail"),
    path("api/gifts/<uuid:gift_id>/duplicate/", views.gift_duplicate, name="gift-duplicate"),
    path("api/gifts/<uuid:gift_id>/checkout/", views.gift_checkout, name="gift-checkout"),
    path("api/gifts/<uuid:gift_id>/refund/", views.gift_refund, name="gift-refund"),
    path("api/gifts/<uuid:gift_id>/media/", views.gift_media_upload, name="gift-media-upload"),
    path("api/gifts/<uuid:gift_id>/media/<uuid:media_id>/", views.gift_media_detail, name="gift-media-detail"),
    path("api/payments/yookassa/webhook/", views.yookassa_webhook, name="yookassa-webhook"),
    path("g/<str:slug>/<str:token>/", views.public_gift, name="public-gift"),
    path("g/<str:slug>/<str:token>/media/<uuid:media_id>/", views.public_media, name="public-media"),
    path("api/public/<str:slug>/<str:token>/open/", views.public_open, name="public-open"),
    path("api/public/<str:slug>/<str:token>/answer/", views.public_answer, name="public-answer"),
]
