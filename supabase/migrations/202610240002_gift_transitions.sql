-- Transactional state transitions. Only trusted Edge Functions may execute these.

create or replace function public.begin_checkout(p_owner_id uuid, p_gift_id uuid)
returns public.payments
language plpgsql security definer set search_path = '' as $$
declare
  v_gift public.gifts%rowtype;
  v_tariff public.tariffs%rowtype;
  v_payment public.payments%rowtype;
  v_photos integer;
  v_videos integer;
  v_email text;
  v_telegram text;
begin
  select * into v_gift from public.gifts where id = p_gift_id for update;
  if not found or v_gift.owner_id <> p_owner_id or v_gift.deleted_at is not null then
    raise exception 'Подарок не найден.';
  end if;
  if v_gift.status = 'awaiting_payment' then
    select * into v_payment from public.payments
      where gift_id = v_gift.id and status = 'pending'
      order by created_at desc limit 1 for update;
    if not found then raise exception 'Платёж не найден.'; end if;
    return v_payment;
  end if;
  if v_gift.status <> 'draft' then raise exception 'Подарок уже нельзя оплатить.'; end if;
  select * into v_tariff from public.tariffs
    where slug = v_gift.tariff_slug and is_active for share;
  if not found then raise exception 'Тариф недоступен.'; end if;
  if v_gift.title = '' or v_gift.message = '' or v_gift.custom_slug is null then
    raise exception 'Заполни заголовок, послание и название ссылки.';
  end if;
  if not (v_tariff.limits->'allowed_templates' ? v_gift.template) then
    raise exception 'Шаблон недоступен в выбранном тарифе.';
  end if;
  if v_gift.music_track <> '' and not (v_tariff.limits->>'allow_music')::boolean then
    raise exception 'Музыка недоступна в выбранном тарифе.';
  end if;
  if v_gift.scheduled_release_at is not null and (
    not (v_tariff.limits->>'allow_scheduled_release')::boolean or
    v_gift.scheduled_release_at <= now()
  ) then raise exception 'Проверь дату выдачи ссылки.'; end if;
  select count(*) filter (where kind = 'photo'), count(*) filter (where kind = 'video')
    into v_photos, v_videos from public.gift_media where gift_id = v_gift.id;
  if v_photos > (v_tariff.limits->>'max_photos')::integer or v_videos > 1 or
    (v_videos > 0 and not (v_tariff.limits->>'allow_video')::boolean) then
    raise exception 'Медиа не соответствует выбранному тарифу.';
  end if;
  if exists (select 1 from public.gift_media where gift_id = v_gift.id and (
    (kind = 'photo' and size_bytes > (v_tariff.limits->>'max_photo_bytes')::bigint) or
    (kind = 'video' and size_bytes > coalesce((v_tariff.limits->>'max_video_bytes')::bigint, 0))
  )) then raise exception 'Файл превышает лимит тарифа.'; end if;
  select email into v_email from auth.users where id = p_owner_id;
  select telegram_id into v_telegram from public.profiles where id = p_owner_id;
  if (v_tariff.limits->>'notify_on_open')::boolean and
    nullif(v_gift.contact_email, '') is null and
    nullif(v_email, '') is null and nullif(v_telegram, '') is null then
    raise exception 'Укажи email для уведомления об открытии.';
  end if;
  insert into public.payments (owner_id, gift_id, tariff_slug, amount_rub)
    values (p_owner_id, v_gift.id, v_tariff.slug, v_tariff.price_rub)
    returning * into v_payment;
  update public.gifts set status = 'awaiting_payment' where id = v_gift.id;
  return v_payment;
end;
$$;

create or replace function public.confirm_payment(
  p_payment_id uuid, p_provider_payment_id text, p_amount_rub integer
)
returns public.gifts
language plpgsql security definer set search_path = '' as $$
declare
  v_payment public.payments%rowtype;
  v_gift public.gifts%rowtype;
  v_days integer;
  v_now timestamptz := now();
begin
  select * into v_payment from public.payments where id = p_payment_id for update;
  if not found or v_payment.amount_rub <> p_amount_rub or
    (v_payment.provider_payment_id is not null and
     v_payment.provider_payment_id <> p_provider_payment_id) then
    raise exception 'Платёж не совпадает.';
  end if;
  select * into v_gift from public.gifts where id = v_payment.gift_id for update;
  if not found or v_gift.owner_id <> v_payment.owner_id or
    v_gift.tariff_slug <> v_payment.tariff_slug then
    raise exception 'Платёж относится к другому подарку.';
  end if;
  if v_payment.status = 'succeeded' then return v_gift; end if;
  if v_payment.status <> 'pending' or v_gift.status <> 'awaiting_payment' then
    raise exception 'Платёж уже нельзя подтвердить.';
  end if;
  select (limits->>'link_days')::integer into v_days
    from public.tariffs where slug = v_payment.tariff_slug;
  update public.payments set
    provider_payment_id = p_provider_payment_id, status = 'succeeded', paid_at = v_now
    where id = v_payment.id;
  if v_gift.scheduled_release_at is not null and v_gift.scheduled_release_at > v_now then
    update public.gifts set status = 'awaiting_link' where id = v_gift.id returning * into v_gift;
  else
    update public.gifts set status = 'link_ready',
      public_token = encode(extensions.gen_random_bytes(32), 'hex'),
      link_available_at = v_now,
      expires_at = v_now + make_interval(days => v_days)
      where id = v_gift.id returning * into v_gift;
  end if;
  return v_gift;
end;
$$;

create or replace function public.release_due_links(p_owner_id uuid)
returns integer
language plpgsql security definer set search_path = '' as $$
declare
  v_gift public.gifts%rowtype;
  v_days integer;
  v_count integer := 0;
  v_now timestamptz := now();
begin
  for v_gift in select g.* from public.gifts g
    where g.owner_id = p_owner_id and g.status = 'awaiting_link'
      and g.scheduled_release_at <= v_now
      and exists (select 1 from public.payments p
        where p.gift_id = g.id and p.status = 'succeeded')
    for update of g skip locked
  loop
    select (limits->>'link_days')::integer into v_days
      from public.tariffs where slug = v_gift.tariff_slug;
    update public.gifts set status = 'link_ready',
      public_token = encode(extensions.gen_random_bytes(32), 'hex'),
      link_available_at = v_now,
      expires_at = v_now + make_interval(days => v_days)
      where id = v_gift.id;
    v_count := v_count + 1;
  end loop;
  return v_count;
end;
$$;

create or replace function public.open_gift(p_slug text, p_token text)
returns table (gift_id uuid, first_open boolean)
language plpgsql security definer set search_path = '' as $$
declare
  v_gift public.gifts%rowtype;
begin
  select * into v_gift from public.gifts
    where lower(custom_slug) = lower(p_slug) and public_token = p_token
    for update;
  if not found or v_gift.deleted_at is not null or
    v_gift.status not in ('link_ready', 'opened', 'answered') or
    v_gift.link_available_at is null or v_gift.expires_at <= now() or
    not exists (select 1 from public.payments p
      where p.gift_id = v_gift.id and p.status = 'succeeded') then
    raise exception 'Ссылка недоступна.';
  end if;
  gift_id := v_gift.id;
  first_open := v_gift.opened_at is null;
  if first_open then
    update public.gifts set opened_at = now(), status = 'opened'
      where id = v_gift.id;
  end if;
  return next;
end;
$$;

create or replace function public.answer_gift(p_slug text, p_token text, p_choice text)
returns public.recipient_responses
language plpgsql security definer set search_path = '' as $$
declare
  v_gift public.gifts%rowtype;
  v_response public.recipient_responses%rowtype;
begin
  if p_choice not in ('yes', 'think') then raise exception 'Неизвестный ответ.'; end if;
  select * into v_gift from public.gifts
    where lower(custom_slug) = lower(p_slug) and public_token = p_token
    for update;
  if not found or v_gift.deleted_at is not null or
    v_gift.status not in ('opened', 'answered') or
    v_gift.opened_at is null or v_gift.expires_at <= now() or
    not exists (select 1 from public.payments p
      where p.gift_id = v_gift.id and p.status = 'succeeded') or
    not exists (select 1 from public.tariffs t where t.slug = v_gift.tariff_slug
      and (t.limits->>'allow_answers')::boolean) then
    raise exception 'Ответ недоступен.';
  end if;
  insert into public.recipient_responses (gift_id, choice)
    values (v_gift.id, p_choice) returning * into v_response;
  update public.gifts set status = 'answered', answered_at = v_response.responded_at
    where id = v_gift.id;
  return v_response;
end;
$$;

create or replace function public.start_refund(p_owner_id uuid, p_gift_id uuid)
returns public.payments
language plpgsql security definer set search_path = '' as $$
declare
  v_gift public.gifts%rowtype;
  v_payment public.payments%rowtype;
begin
  select * into v_gift from public.gifts where id = p_gift_id for update;
  if not found or v_gift.owner_id <> p_owner_id or v_gift.deleted_at is not null or
    v_gift.opened_at is not null then raise exception 'Возврат недоступен.'; end if;
  select * into v_payment from public.payments
    where gift_id = p_gift_id and owner_id = p_owner_id
      and status in ('succeeded', 'refunded')
    order by created_at desc limit 1 for update;
  if not found or v_payment.provider_payment_id is null then
    raise exception 'Подтверждённый платёж не найден.';
  end if;
  if v_payment.status = 'refunded' then return v_payment; end if;
  update public.payments set refund_requested_at = coalesce(refund_requested_at, now())
    where id = v_payment.id returning * into v_payment;
  update public.gifts set status = 'cancelled' where id = p_gift_id;
  return v_payment;
end;
$$;

create or replace function public.complete_refund(p_payment_id uuid, p_provider_refund_id text)
returns public.payments
language plpgsql security definer set search_path = '' as $$
declare
  v_payment public.payments%rowtype;
  v_gift public.gifts%rowtype;
begin
  select * into v_payment from public.payments where id = p_payment_id for update;
  if not found or (v_payment.provider_refund_id is not null and
    v_payment.provider_refund_id <> p_provider_refund_id) then
    raise exception 'Возврат не совпадает.';
  end if;
  select * into v_gift from public.gifts where id = v_payment.gift_id for update;
  if v_gift.opened_at is not null then raise exception 'Подарок уже открыт.'; end if;
  if v_payment.status = 'refunded' then return v_payment; end if;
  if v_payment.status <> 'succeeded' then raise exception 'Возврат недоступен.'; end if;
  update public.payments set status = 'refunded',
    provider_refund_id = p_provider_refund_id, refunded_at = now()
    where id = p_payment_id returning * into v_payment;
  update public.gifts set status = 'refunded', public_token = null
    where id = v_gift.id;
  return v_payment;
end;
$$;

create or replace function public.cancel_refund(p_payment_id uuid)
returns void
language plpgsql security definer set search_path = '' as $$
declare
  v_payment public.payments%rowtype;
  v_gift public.gifts%rowtype;
begin
  select * into v_payment from public.payments where id = p_payment_id for update;
  if not found or v_payment.status <> 'succeeded' then return; end if;
  select * into v_gift from public.gifts where id = v_payment.gift_id for update;
  if v_gift.status <> 'cancelled' or v_gift.opened_at is not null then return; end if;
  update public.payments set refund_requested_at = null, provider_refund_id = null,
    refund_idempotence_key = gen_random_uuid() where id = p_payment_id;
  update public.gifts set status = case
    when link_available_at is null then 'awaiting_link'
    when expires_at <= now() then 'expired'
    else 'link_ready' end where id = v_gift.id;
end;
$$;

revoke all on function public.begin_checkout(uuid, uuid) from public, anon, authenticated;
revoke all on function public.confirm_payment(uuid, text, integer) from public, anon, authenticated;
revoke all on function public.release_due_links(uuid) from public, anon, authenticated;
revoke all on function public.open_gift(text, text) from public, anon, authenticated;
revoke all on function public.answer_gift(text, text, text) from public, anon, authenticated;
revoke all on function public.start_refund(uuid, uuid) from public, anon, authenticated;
revoke all on function public.complete_refund(uuid, text) from public, anon, authenticated;
revoke all on function public.cancel_refund(uuid) from public, anon, authenticated;
grant execute on function public.begin_checkout(uuid, uuid) to service_role;
grant execute on function public.confirm_payment(uuid, text, integer) to service_role;
grant execute on function public.release_due_links(uuid) to service_role;
grant execute on function public.open_gift(text, text) to service_role;
grant execute on function public.answer_gift(text, text, text) to service_role;
grant execute on function public.start_refund(uuid, uuid) to service_role;
grant execute on function public.complete_refund(uuid, text) to service_role;
grant execute on function public.cancel_refund(uuid) to service_role;
