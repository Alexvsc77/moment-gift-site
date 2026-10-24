import "jsr:@supabase/functions-js/edge-runtime.d.ts";
import { createClient } from "npm:@supabase/supabase-js@2";

type Json = Record<string, unknown>;
type AuthUser = { id: string; email?: string | null; user_metadata?: Json };

const supabaseUrl = Deno.env.get("SUPABASE_URL") ?? "";
const modernKeys = JSON.parse(Deno.env.get("SUPABASE_SECRET_KEYS") || "{}");
const serviceKey = modernKeys.default || Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") || "";
const siteUrl = (Deno.env.get("MOMENT_SITE_URL") ||
  "https://alexvsc77.github.io/moment-gift-site/").replace(/\/?$/, "/");
const allowedOrigins = new Set([
  new URL(siteUrl).origin,
  ...(Deno.env.get("MOMENT_EXTRA_ORIGINS") || "").split(",").map((value) => value.trim()).filter(Boolean),
]);
const admin = createClient(supabaseUrl, serviceKey, {
  auth: { persistSession: false, autoRefreshToken: false },
});

class ApiError extends Error {
  constructor(message: string, public status = 400) { super(message); }
}

function cors(req: Request): HeadersInit {
  const origin = req.headers.get("origin");
  return {
    ...(origin && allowedOrigins.has(origin) ? { "Access-Control-Allow-Origin": origin } : {}),
    "Access-Control-Allow-Headers": "authorization, apikey, content-type, x-client-info",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Vary": "Origin",
    "Cache-Control": "no-store",
  };
}

function json(req: Request, body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...cors(req), "Content-Type": "application/json; charset=utf-8" },
  });
}

function requiredString(value: unknown, label: string, max: number, required = false): string {
  if (typeof value !== "string") throw new ApiError(`${label}: нужен текст.`);
  const text = value.trim();
  if (required && !text) throw new ApiError(`Заполни поле «${label}».`);
  if (text.length > max) throw new ApiError(`${label}: максимум ${max} символов.`);
  return text;
}

function email(value: unknown): string {
  if (value === null || value === undefined || value === "") return "";
  const text = requiredString(value, "Email", 254).toLowerCase();
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(text)) throw new ApiError("Укажи корректный email.");
  return text;
}

function uuid(value: unknown): string {
  if (typeof value !== "string" || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value)) {
    throw new ApiError("Некорректный идентификатор.");
  }
  return value;
}

function checked(result: { data: any; error: any }, message = "Не удалось выполнить действие."): any {
  if (result.error) {
    console.error(message, result.error.message);
    if (result.error.code === "23505") throw new ApiError("Название ссылки уже занято или запись уже существует.", 409);
    throw new ApiError(message);
  }
  return result.data;
}

async function authUser(req: Request): Promise<AuthUser> {
  const match = /^Bearer (.+)$/i.exec(req.headers.get("authorization") || "");
  if (!match) throw new ApiError("Нужно войти в аккаунт.", 401);
  const { data, error } = await admin.auth.getUser(match[1]);
  if (error || !data.user) throw new ApiError("Сессия закончилась. Войди снова.", 401);
  return data.user as AuthUser;
}

async function tariff(slug: string): Promise<any> {
  const row = checked(await admin.from("tariffs").select("*").eq("slug", slug).eq("is_active", true).maybeSingle(), "Тариф недоступен.");
  if (!row) throw new ApiError("Тариф недоступен.");
  return row;
}

async function ownedGift(ownerId: string, giftId: unknown): Promise<any> {
  const gift = checked(await admin.from("gifts").select("*").eq("id", uuid(giftId)).eq("owner_id", ownerId).is("deleted_at", null).maybeSingle(), "Не удалось загрузить подарок.");
  if (!gift) throw new ApiError("Подарок не найден.", 404);
  return gift;
}

async function signedMedia(giftId: string): Promise<any[]> {
  const media = checked(await admin.from("gift_media").select("*").eq("gift_id", giftId).order("sort_order"), "Не удалось загрузить медиа.");
  return await Promise.all((media || []).map(async (item: any) => {
    const data = checked(await admin.storage.from("gift-media").createSignedUrl(item.storage_path, 600), "Не удалось открыть файл.");
    return { id: item.id, kind: item.kind, size_bytes: item.size_bytes, url: data.signedUrl };
  }));
}

function giftLink(gift: any): string | null {
  if (!gift.public_token || !gift.custom_slug || !gift.link_available_at ||
    !["link_ready", "opened", "answered"].includes(gift.status) ||
    !gift.expires_at || Date.parse(gift.expires_at) <= Date.now()) return null;
  return `${siteUrl}gift.html#${encodeURIComponent(gift.custom_slug)}/${gift.public_token}`;
}

async function serializeGift(gift: any, withMedia = true): Promise<Json> {
  const answer = checked(await admin.from("recipient_responses").select("choice,responded_at").eq("gift_id", gift.id).maybeSingle());
  return {
    id: gift.id, status: gift.status, tariff: gift.tariff_slug,
    recipient: gift.recipient_name, title: gift.title, message: gift.message,
    sender: gift.sender_name, occasion: gift.occasion,
    contact_email: gift.contact_email, template: gift.template,
    music_track: gift.music_track, custom_slug: gift.custom_slug,
    scheduled_release_at: gift.scheduled_release_at,
    opened_at: gift.opened_at, answer,
    link: giftLink(gift),
    media: withMedia ? await signedMedia(gift.id) : [],
  };
}

async function saveDraft(user: AuthUser, body: Json): Promise<Json> {
  const payload = (body.payload && typeof body.payload === "object") ? body.payload as Json : body;
  const plan = requiredString(payload.plan ?? "standard", "Тариф", 40);
  const selectedTariff = await tariff(plan);
  const limits = selectedTariff.limits as Json;
  const template = requiredString(payload.template ?? "petal", "Шаблон", 20);
  if (!(limits.allowed_templates as string[]).includes(template)) throw new ApiError("Шаблон недоступен в выбранном тарифе.");
  const music = requiredString(payload.music_track ?? "", "Музыка", 20);
  if (!["", "calm", "warm", "bright"].includes(music) || (music && !limits.allow_music)) throw new ApiError("Музыка недоступна в выбранном тарифе.");
  const rawSlug = requiredString(payload.custom_slug ?? "", "Название ссылки", 60);
  const slug = rawSlug.toLocaleLowerCase("ru-RU").replace(/\s+/g, "-");
  if (slug && !/^[\p{L}\p{N}_-]{1,60}$/u.test(slug)) throw new ApiError("Используй в названии ссылки буквы, цифры и дефис.");
  let schedule: string | null = null;
  if (payload.scheduled_release_at) {
    if (!limits.allow_scheduled_release) throw new ApiError("Отложенная выдача доступна только в Премиуме.");
    const time = Date.parse(String(payload.scheduled_release_at));
    if (!Number.isFinite(time) || time <= Date.now()) throw new ApiError("Выбери время выдачи в будущем.");
    schedule = new Date(time).toISOString();
  }
  const values = {
    tariff_slug: plan, template, music_track: music,
    recipient_name: requiredString(payload.recipient ?? "", "Получатель", 50),
    title: requiredString(payload.title ?? "", "Заголовок", 70),
    message: requiredString(payload.message ?? "", "Послание", 600),
    sender_name: requiredString(payload.sender ?? "", "Отправитель", 50),
    occasion: requiredString(payload.occasion ?? "", "Повод", 50),
    contact_email: email(payload.contact_email),
    custom_slug: slug || null, scheduled_release_at: schedule,
  };
  let gift: any;
  if (body.id) {
    const current = await ownedGift(user.id, body.id);
    if (current.status !== "draft") throw new ApiError("Этот подарок уже нельзя редактировать.");
    gift = checked(await admin.from("gifts").update(values).eq("id", current.id).eq("status", "draft").select("*").single(), "Не удалось сохранить черновик.");
  } else {
    gift = checked(await admin.from("gifts").insert({ ...values, owner_id: user.id }).select("*").single(), "Не удалось создать черновик.");
  }
  return { gift: await serializeGift(gift) };
}

async function listGifts(user: AuthUser): Promise<Json> {
  checked(await admin.rpc("release_due_links", { p_owner_id: user.id }), "Не удалось проверить расписание.");
  const gifts = checked(await admin.from("gifts").select("*").eq("owner_id", user.id).is("deleted_at", null).order("created_at", { ascending: false }), "Не удалось загрузить подарки.");
  return { gifts: await Promise.all((gifts || []).map((gift: any) => serializeGift(gift, false))) };
}

async function listPayments(user: AuthUser): Promise<Json> {
  const payments = checked(await admin.from("payments").select("*, gifts(title), tariffs(name)").eq("owner_id", user.id).order("created_at", { ascending: false }), "Не удалось загрузить платежи.");
  return { payments: (payments || []).map((payment: any) => ({
    id: payment.id, gift_id: payment.gift_id,
    gift_title: payment.gifts?.title || "Подарок", tariff: payment.tariffs?.name || payment.tariff_slug,
    amount: payment.amount_rub, status: payment.status,
    refund_requested_at: payment.refund_requested_at,
    provider_refund_id: payment.provider_refund_id,
    created_at: payment.created_at,
  })) };
}

async function duplicateGift(user: AuthUser, giftId: unknown): Promise<Json> {
  const source = await ownedGift(user.id, giftId);
  const { id: _id, ...unused } = source;
  void unused;
  const copy = checked(await admin.from("gifts").insert({
    owner_id: user.id, tariff_slug: source.tariff_slug,
    recipient_name: source.recipient_name, title: source.title, message: source.message,
    sender_name: source.sender_name, occasion: source.occasion,
    contact_email: source.contact_email, template: source.template,
    music_track: source.music_track,
  }).select("*").single(), "Не удалось скопировать подарок.");
  const media = checked(await admin.from("gift_media").select("*").eq("gift_id", source.id));
  for (const item of media || []) {
    const extension = item.storage_path.split(".").pop();
    const path = `${copy.id}/${crypto.randomUUID()}.${extension}`;
    checked(await admin.storage.from("gift-media").copy(item.storage_path, path), "Не удалось скопировать файл.");
    checked(await admin.from("gift_media").insert({
      gift_id: copy.id, storage_path: path, kind: item.kind,
      size_bytes: item.size_bytes, mime_type: item.mime_type, sort_order: item.sort_order,
    }), "Не удалось записать копию файла.");
  }
  return { gift: await serializeGift(copy) };
}

async function deleteGift(user: AuthUser, giftId: unknown): Promise<Json> {
  const gift = await ownedGift(user.id, giftId);
  if (gift.status === "awaiting_payment") throw new ApiError("Сначала дождись завершения платежа.");
  if (!gift.opened_at) {
    const paid = checked(await admin.from("payments").select("id").eq("gift_id", gift.id).eq("status", "succeeded").limit(1));
    if (paid.length) throw new ApiError("Перед удалением ещё не открытого подарка оформи возврат.");
  }
  checked(await admin.from("gifts").update({ deleted_at: new Date().toISOString() }).eq("id", gift.id));
  return { ok: true };
}

function mediaLimit(tariffRow: any, kind: string): number {
  if (kind === "photo") return Number(tariffRow.limits.max_photo_bytes || 0);
  if (kind === "video" && tariffRow.limits.allow_video) return Number(tariffRow.limits.max_video_bytes || 0);
  throw new ApiError("Видео недоступно в выбранном тарифе.");
}

async function prepareMedia(user: AuthUser, body: Json): Promise<Json> {
  const gift = await ownedGift(user.id, body.gift_id);
  if (gift.status !== "draft") throw new ApiError("Медиа можно добавить только в черновик.");
  const selectedTariff = await tariff(gift.tariff_slug);
  const kind = requiredString(body.kind, "Тип файла", 10);
  const mime = requiredString(body.mime_type, "Формат файла", 50);
  const accepted = kind === "photo" ? ["image/jpeg", "image/png", "image/webp"] : ["video/mp4"];
  if (!accepted.includes(mime)) throw new ApiError("Этот формат файла не поддерживается.");
  const size = Number(body.size_bytes);
  if (!Number.isSafeInteger(size) || size <= 0 || size > mediaLimit(selectedTariff, kind)) throw new ApiError("Файл превышает лимит тарифа.");
  const existing = checked(await admin.from("gift_media").select("id,kind").eq("gift_id", gift.id));
  if (kind === "photo" && existing.filter((item: any) => item.kind === "photo").length >= Number(selectedTariff.limits.max_photos)) throw new ApiError("Достигнут лимит фотографий.");
  if (kind === "video" && existing.some((item: any) => item.kind === "video")) throw new ApiError("Можно добавить только одно видео.");
  const extension = { "image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "video/mp4": "mp4" }[mime] || "bin";
  const path = `${gift.id}/${crypto.randomUUID()}.${extension}`;
  const signed = checked(await admin.storage.from("gift-media").createSignedUploadUrl(path), "Не удалось подготовить загрузку.");
  return { path, token: signed.token, bucket: "gift-media" };
}

async function completeMedia(user: AuthUser, body: Json): Promise<Json> {
  const gift = await ownedGift(user.id, body.gift_id);
  if (gift.status !== "draft") throw new ApiError("Медиа можно добавить только в черновик.");
  const path = requiredString(body.path, "Путь файла", 200);
  if (!path.startsWith(`${gift.id}/`) || !/^[0-9a-f-]{36}\.(jpg|png|webp|mp4)$/.test(path.slice(gift.id.length + 1))) throw new ApiError("Некорректный путь файла.");
  const info = checked(await admin.storage.from("gift-media").info(path), "Загруженный файл не найден.");
  const mime = String(info.contentType || info.mimetype || "");
  const kind = mime.startsWith("image/") ? "photo" : "video";
  const selectedTariff = await tariff(gift.tariff_slug);
  const size = Number(info.size);
  if (!["image/jpeg", "image/png", "image/webp", "video/mp4"].includes(mime) ||
    !Number.isSafeInteger(size) || size <= 0 || size > mediaLimit(selectedTariff, kind)) {
    await admin.storage.from("gift-media").remove([path]);
    throw new ApiError("Загруженный файл не соответствует тарифу.");
  }
  const existing = checked(await admin.from("gift_media").select("id,kind").eq("gift_id", gift.id));
  if ((kind === "photo" && existing.filter((item: any) => item.kind === "photo").length >= Number(selectedTariff.limits.max_photos)) ||
    (kind === "video" && (!selectedTariff.limits.allow_video || existing.some((item: any) => item.kind === "video")))) throw new ApiError("Достигнут лимит файлов.");
  const media = checked(await admin.from("gift_media").insert({
    gift_id: gift.id, storage_path: path, kind, mime_type: mime,
    size_bytes: size, sort_order: existing.length,
  }).select("id,kind,size_bytes").single(), "Не удалось сохранить файл.");
  return { media };
}

async function deleteMedia(user: AuthUser, body: Json): Promise<Json> {
  const gift = await ownedGift(user.id, body.gift_id);
  if (gift.status !== "draft") throw new ApiError("Файлы опубликованного подарка нельзя удалить.");
  const media = checked(await admin.from("gift_media").select("*").eq("id", uuid(body.media_id)).eq("gift_id", gift.id).maybeSingle());
  if (!media) throw new ApiError("Файл не найден.", 404);
  checked(await admin.from("gift_media").delete().eq("id", media.id));
  checked(await admin.storage.from("gift-media").remove([media.storage_path]));
  return { ok: true };
}

function yookassaCredentials(): string {
  const shop = Deno.env.get("YOOKASSA_SHOP_ID");
  const secret = Deno.env.get("YOOKASSA_SECRET_KEY");
  if (!shop || !secret) throw new ApiError("Оплата пока не настроена. Обратись к владельцу сайта.", 503);
  return `Basic ${btoa(`${shop}:${secret}`)}`;
}

async function yookassa(method: string, path: string, payload?: Json, idempotenceKey?: string): Promise<any> {
  const response = await fetch(`https://api.yookassa.ru/v3${path}`, {
    method,
    headers: {
      Authorization: yookassaCredentials(),
      Accept: "application/json", "Content-Type": "application/json",
      ...(idempotenceKey ? { "Idempotence-Key": idempotenceKey } : {}),
    },
    body: payload ? JSON.stringify(payload) : undefined,
    signal: AbortSignal.timeout(12000),
  });
  if (!response.ok) {
    console.error("YooKassa request failed", response.status, path);
    throw new ApiError("Платёжный сервис временно недоступен.", 502);
  }
  return await response.json();
}

function providerAmount(value: any, expectedRub: number): boolean {
  return value?.currency === "RUB" && Number(value.value) === expectedRub;
}

async function checkout(user: AuthUser, body: Json): Promise<Json> {
  yookassaCredentials();
  const gift = await ownedGift(user.id, body.gift_id);
  const payment = checked(await admin.rpc("begin_checkout", { p_owner_id: user.id, p_gift_id: gift.id }), "Подарок не готов к оплате.");
  const receiptEmail = email(body.receipt_email || gift.contact_email || user.email || "");
  let provider: any;
  if (payment.provider_payment_id) {
    provider = await yookassa("GET", `/payments/${encodeURIComponent(payment.provider_payment_id)}`);
  } else {
    const selectedTariff = await tariff(payment.tariff_slug);
    const payload: Json = {
      amount: { value: Number(payment.amount_rub).toFixed(2), currency: "RUB" },
      capture: true,
      confirmation: { type: "redirect", return_url: `${siteUrl}#account` },
      description: `Создание подарка ${gift.id.slice(0, 8)}`,
      metadata: { payment_id: payment.id, gift_id: gift.id },
    };
    if (Deno.env.get("YOOKASSA_SEND_RECEIPT") === "1") {
      const vatCode = Number(Deno.env.get("YOOKASSA_VAT_CODE"));
      if (!receiptEmail || !Number.isInteger(vatCode) || vatCode < 1) throw new ApiError("Для чека нужен email и код НДС.");
      payload.receipt = {
        customer: { email: receiptEmail },
        items: [{ description: `Создание подарка, тариф ${selectedTariff.name}`,
          quantity: "1.00", amount: payload.amount, vat_code: vatCode,
          payment_subject: "service", payment_mode: "full_payment" }],
      };
    }
    provider = await yookassa("POST", "/payments", payload, payment.idempotence_key);
  }
  if (!provider.id || !providerAmount(provider.amount, Number(payment.amount_rub))) throw new ApiError("Платёжный сервис вернул неверные данные.", 502);
  if (payment.provider_payment_id && payment.provider_payment_id !== provider.id) throw new ApiError("ID платежа изменился.", 502);
  checked(await admin.from("payments").update({ provider_payment_id: provider.id }).eq("id", payment.id).in("status", ["pending", "succeeded"]), "Не удалось сохранить ID платежа.");
  const confirmation = provider.status === "pending" ? provider.confirmation?.confirmation_url : null;
  if (confirmation && !/^https:\/\//.test(confirmation)) throw new ApiError("Не удалось получить безопасную ссылку оплаты.", 502);
  return { payment_id: payment.id, confirmation_url: confirmation };
}

async function refund(user: AuthUser, body: Json): Promise<Json> {
  yookassaCredentials();
  const gift = await ownedGift(user.id, body.gift_id);
  const payment = checked(await admin.rpc("start_refund", { p_owner_id: user.id, p_gift_id: gift.id }), "Возврат недоступен.");
  if (payment.status === "refunded") return { refund_status: "succeeded" };
  let provider: any;
  if (payment.provider_refund_id) {
    provider = await yookassa("GET", `/refunds/${encodeURIComponent(payment.provider_refund_id)}`);
  } else {
    provider = await yookassa("POST", "/refunds", {
      payment_id: payment.provider_payment_id,
      amount: { value: Number(payment.amount_rub).toFixed(2), currency: "RUB" },
    }, payment.refund_idempotence_key);
  }
  if (!provider.id || provider.payment_id !== payment.provider_payment_id ||
    !providerAmount(provider.amount, Number(payment.amount_rub))) throw new ApiError("Возврат не совпадает с платежом.", 502);
  checked(await admin.from("payments").update({ provider_refund_id: provider.id }).eq("id", payment.id), "Не удалось сохранить ID возврата.");
  const fresh = await yookassa("GET", `/refunds/${encodeURIComponent(provider.id)}`);
  if (fresh.id !== provider.id || fresh.payment_id !== payment.provider_payment_id ||
    !providerAmount(fresh.amount, Number(payment.amount_rub))) throw new ApiError("Статус возврата не подтверждён.", 502);
  if (fresh.status === "succeeded") checked(await admin.rpc("complete_refund", { p_payment_id: payment.id, p_provider_refund_id: fresh.id }));
  else if (fresh.status === "canceled") checked(await admin.rpc("cancel_refund", { p_payment_id: payment.id }));
  else if (fresh.status !== "pending") throw new ApiError("Неизвестный статус возврата.", 502);
  return { refund_status: fresh.status };
}

async function notifyFirstOpen(gift: any): Promise<void> {
  if (gift.tariff_slug !== "premium" || gift.open_notification_sent_at) return;
  const profile = checked(await admin.from("profiles").select("telegram_id,contact_email").eq("id", gift.owner_id).maybeSingle());
  const owner = await admin.auth.admin.getUserById(gift.owner_id);
  const destination = gift.contact_email || profile?.contact_email || owner.data.user?.email;
  const text = `Твой подарок «${gift.title}» открыли.`;
  let sent = false;
  if (destination && Deno.env.get("RESEND_API_KEY") && Deno.env.get("MOMENT_FROM_EMAIL")) {
    const response = await fetch("https://api.resend.com/emails", {
      method: "POST",
      headers: { Authorization: `Bearer ${Deno.env.get("RESEND_API_KEY")}`, "Content-Type": "application/json" },
      body: JSON.stringify({ from: Deno.env.get("MOMENT_FROM_EMAIL"), to: [destination], subject: "Подарок открыли · Момент", text }),
      signal: AbortSignal.timeout(8000),
    });
    sent = response.ok;
  } else if (profile?.telegram_id && Deno.env.get("TELEGRAM_BOT_TOKEN")) {
    const response = await fetch(`https://api.telegram.org/bot${Deno.env.get("TELEGRAM_BOT_TOKEN")}/sendMessage`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chat_id: profile.telegram_id, text }),
      signal: AbortSignal.timeout(8000),
    });
    sent = response.ok;
  }
  if (sent) checked(await admin.from("gifts").update({ open_notification_sent_at: new Date().toISOString() }).eq("id", gift.id).is("open_notification_sent_at", null));
}

async function openGift(body: Json): Promise<Json> {
  const slug = requiredString(body.slug, "Ссылка", 60, true);
  const token = requiredString(body.token, "Секрет ссылки", 64, true);
  if (!/^[a-f0-9]{64}$/.test(token)) throw new ApiError("Ссылка недоступна.", 404);
  const opened = checked(await admin.rpc("open_gift", { p_slug: slug, p_token: token }), "Ссылка недоступна.");
  const giftId = opened?.[0]?.gift_id;
  if (!giftId) throw new ApiError("Ссылка недоступна.", 404);
  const gift = checked(await admin.from("gifts").select("*").eq("id", giftId).single());
  const selectedTariff = await tariff(gift.tariff_slug);
  const media = await signedMedia(gift.id);
  if (opened[0].first_open) EdgeRuntime.waitUntil(notifyFirstOpen(gift).catch((error) => console.error("Open notification failed", error)));
  return {
    template: gift.template, music_track: gift.music_track,
    recipient: gift.recipient_name, title: gift.title, message: gift.message,
    sender: gift.sender_name, occasion: gift.occasion, media,
    allow_answers: Boolean(selectedTariff.limits.allow_answers),
    service_signature: Boolean(selectedTariff.limits.service_signature),
  };
}

async function answerGift(body: Json): Promise<Json> {
  const slug = requiredString(body.slug, "Ссылка", 60, true);
  const token = requiredString(body.token, "Секрет ссылки", 64, true);
  const choice = requiredString(body.choice, "Ответ", 10);
  if (!/^[a-f0-9]{64}$/.test(token)) throw new ApiError("Ссылка недоступна.", 404);
  checked(await admin.rpc("answer_gift", { p_slug: slug, p_token: token, p_choice: choice }), "Не удалось записать ответ.");
  return { ok: true };
}

async function webhook(body: Json): Promise<Json> {
  if (body.type !== "notification" || !body.object || typeof body.object !== "object") throw new ApiError("Некорректное уведомление.");
  const event = String(body.event || "");
  const object = body.object as Json;
  const providerId = requiredString(object.id, "ID события", 100, true);
  if (event === "refund.succeeded") {
    const fresh = await yookassa("GET", `/refunds/${encodeURIComponent(providerId)}`);
    if (fresh.id !== providerId || fresh.status !== "succeeded") throw new ApiError("Статус возврата не совпадает.", 502);
    const payment = checked(await admin.from("payments").select("*").eq("provider_payment_id", fresh.payment_id).maybeSingle());
    if (!payment || !providerAmount(fresh.amount, Number(payment.amount_rub))) throw new ApiError("Возврат не совпадает с платежом.", 502);
    checked(await admin.rpc("complete_refund", { p_payment_id: payment.id, p_provider_refund_id: providerId }));
    return { status: "refunded" };
  }
  if (!["payment.succeeded", "payment.canceled"].includes(event)) return { status: "ignored" };
  const fresh = await yookassa("GET", `/payments/${encodeURIComponent(providerId)}`);
  if (fresh.id !== providerId || !fresh.metadata?.payment_id || !fresh.metadata?.gift_id) throw new ApiError("Платёж не совпадает.", 502);
  const payment = checked(await admin.from("payments").select("*").eq("id", fresh.metadata.payment_id).maybeSingle());
  if (!payment || payment.gift_id !== fresh.metadata.gift_id ||
    (payment.provider_payment_id && payment.provider_payment_id !== providerId) ||
    !providerAmount(fresh.amount, Number(payment.amount_rub))) throw new ApiError("Платёж не совпадает.", 502);
  if (event === "payment.succeeded" && fresh.status === "succeeded") {
    checked(await admin.rpc("confirm_payment", {
      p_payment_id: payment.id, p_provider_payment_id: providerId,
      p_amount_rub: Number(payment.amount_rub),
    }), "Не удалось подтвердить платёж.");
    return { status: "confirmed" };
  }
  if (event === "payment.canceled" && fresh.status === "canceled") {
    checked(await admin.from("payments").update({ status: "cancelled" }).eq("id", payment.id).eq("status", "pending"));
    checked(await admin.from("gifts").update({ status: "draft" }).eq("id", payment.gift_id).eq("status", "awaiting_payment"));
    return { status: "canceled" };
  }
  throw new ApiError("Статус события не подтверждён.", 502);
}

Deno.serve(async (req: Request): Promise<Response> => {
  if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: cors(req) });
  const origin = req.headers.get("origin");
  if (origin && !allowedOrigins.has(origin)) return json(req, { error: "Доступ запрещён." }, 403);
  if (req.method !== "POST") return json(req, { error: "Используй POST." }, 405);
  try {
    if (!supabaseUrl || !serviceKey) throw new ApiError("Сервер не настроен.", 503);
    if (Number(req.headers.get("content-length") || 0) > 1_000_000) throw new ApiError("Запрос слишком большой.", 413);
    const body = await req.json() as Json;
    if (!body || typeof body !== "object" || Array.isArray(body)) throw new ApiError("Некорректный запрос.");
    const action = String(body.action || "");
    if (action === "webhook" || (body.type === "notification" && typeof body.event === "string")) {
      return json(req, await webhook(body));
    }
    if (action === "gift.open") return json(req, await openGift(body));
    if (action === "gift.answer") return json(req, await answerGift(body));
    if (action === "tariffs") {
      const rows = checked(await admin.from("tariffs").select("slug,name,price_rub,limits").eq("is_active", true).order("sort_order"));
      return json(req, { tariffs: rows.map((row: any) => ({ ...row, price: row.price_rub })) });
    }
    const user = await authUser(req);
    switch (action) {
      case "account": return json(req, { id: user.id, email: user.email || "", display_name: user.user_metadata?.full_name || "" });
      case "gifts.list": return json(req, await listGifts(user));
      case "payments.list": return json(req, await listPayments(user));
      case "gifts.get": return json(req, { gift: await serializeGift(await ownedGift(user.id, body.gift_id)) });
      case "gifts.save": return json(req, await saveDraft(user, body));
      case "gifts.duplicate": return json(req, await duplicateGift(user, body.gift_id));
      case "gifts.delete": return json(req, await deleteGift(user, body.gift_id));
      case "media.prepare": return json(req, await prepareMedia(user, body));
      case "media.complete": return json(req, await completeMedia(user, body));
      case "media.delete": return json(req, await deleteMedia(user, body));
      case "checkout": return json(req, await checkout(user, body));
      case "refund": return json(req, await refund(user, body));
      default: throw new ApiError("Неизвестное действие.", 404);
    }
  } catch (error) {
    if (error instanceof ApiError) return json(req, { error: error.message }, error.status);
    console.error("moment-api error", error);
    return json(req, { error: "Внутренняя ошибка сервера." }, 500);
  }
});
