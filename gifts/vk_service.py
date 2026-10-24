"""Server-side VK ID login with PKCE and a one-use session state."""

import base64
import hashlib
import hmac
import json
import os
import secrets
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import User


VK_AUTHORIZE_URL = "https://id.vk.ru/authorize"
VK_TOKEN_URL = "https://id.vk.ru/oauth2/auth"
VK_LOGIN_MAX_AGE = 600
VK_CALLBACK_PATH = "/api/auth/vk/callback/"


class VKLoginError(Exception):
    """VK ID did not provide a valid authorization for this browser session."""


def _app_id():
    app_id = os.getenv("VK_ID_APP_ID", "")
    if not app_id.isascii() or not app_id.isdigit() or not app_id:
        raise ImproperlyConfigured("VK_ID_APP_ID must contain a VK ID application number.")
    return app_id


def _callback_url(request):
    url = request.build_absolute_uri(VK_CALLBACK_PATH)
    if not settings.DEBUG and not url.startswith("https://"):
        raise ImproperlyConfigured("VK ID callback requires HTTPS outside development.")
    return url


def start_vk_login(request):
    """Store PKCE verifier in the server-side session and return VK's login URL."""

    app_id = _app_id()
    redirect_uri = _callback_url(request)
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()

    request.session["vk_login"] = {
        "state": state,
        "verifier": verifier,
        "redirect_uri": redirect_uri,
        "created_at": int(timezone.now().timestamp()),
    }
    params = {
        "client_id": app_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "code_challenge": challenge,
        "code_challenge_method": "s256",
        "state": state,
    }
    return f"{VK_AUTHORIZE_URL}?{urlencode(params)}"


def _required_callback_value(request, name):
    value = request.GET.get(name)
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise VKLoginError("Вход через VK ID не завершён. Попробуй ещё раз.")
    return value


def complete_vk_login(request):
    """Exchange the callback code once; trust only the verified token response."""

    pending = request.session.pop("vk_login", None)
    if not isinstance(pending, dict):
        raise VKLoginError("Сеанс входа через VK ID не найден. Попробуй ещё раз.")

    now = int(timezone.now().timestamp())
    created_at = pending.get("created_at")
    expected_state = pending.get("state")
    verifier = pending.get("verifier")
    redirect_uri = pending.get("redirect_uri")
    state = _required_callback_value(request, "state")
    if (
        not isinstance(created_at, int)
        or now - created_at < -30
        or now - created_at > VK_LOGIN_MAX_AGE
        or not isinstance(expected_state, str)
        or not hmac.compare_digest(state, expected_state)
        or not isinstance(verifier, str)
        or not isinstance(redirect_uri, str)
        or redirect_uri != _callback_url(request)
    ):
        raise VKLoginError("Вход через VK ID устарел или был прерван. Попробуй ещё раз.")

    code = _required_callback_value(request, "code")
    device_id = _required_callback_value(request, "device_id")
    params = {
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
        "client_id": _app_id(),
        "code_verifier": verifier,
        "state": expected_state,
        "device_id": device_id,
    }
    token_request = Request(
        f"{VK_TOKEN_URL}?{urlencode(params)}",
        data=urlencode({"code": code}).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urlopen(token_request, timeout=8) as response:
            result = json.loads(response.read(16_384).decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, ValueError, UnicodeDecodeError) as exc:
        raise VKLoginError("VK ID временно недоступен. Попробуй ещё раз.") from exc

    if not isinstance(result, dict) or result.get("error"):
        raise VKLoginError("VK ID не подтвердил вход. Попробуй ещё раз.")
    returned_state = result.get("state")
    user_id = result.get("user_id")
    if (
        not isinstance(returned_state, str)
        or not hmac.compare_digest(returned_state, expected_state)
        or isinstance(user_id, bool)
        or not str(user_id).isascii()
        or not str(user_id).isdigit()
        or int(user_id) <= 0
        or len(str(user_id)) > 64
        or not isinstance(result.get("access_token"), str)
        or not result["access_token"]
    ):
        raise VKLoginError("VK ID вернул некорректное подтверждение входа.")

    vk_id = str(user_id)
    with transaction.atomic():
        try:
            user = User.objects.get(vk_id=vk_id)
        except User.DoesNotExist:
            try:
                with transaction.atomic():
                    user = User.objects.create_user(vk_id=vk_id)
            except IntegrityError:
                user = User.objects.get(vk_id=vk_id)
        if not user.is_active:
            raise VKLoginError("Вход в этот аккаунт недоступен.")
    return user
