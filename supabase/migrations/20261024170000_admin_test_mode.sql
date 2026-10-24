-- Admin-only test gifts. Real gifts still require a confirmed payment.
create table public.admin_users (
  user_id uuid primary key references auth.users(id) on delete cascade,
  created_at timestamptz not null default now()
);
alter table public.admin_users enable row level security;
revoke all on public.admin_users from public, anon, authenticated;
grant select on public.admin_users to service_role;

alter table public.gifts add column is_test boolean not null default false;

create or replace function public.publish_test_gift(p_actor_id uuid, p_gift_id uuid)
returns public.gifts
language plpgsql security definer set search_path = '' as $$
declare
  v_gift public.gifts%rowtype;
  v_tariff public.tariffs%rowtype;
  v_photos integer;
  v_videos integer;
  v_now timestamptz := now();
begin
  if not exists (select 1 from public.admin_users where user_id = p_actor_id) then
    raise exception 'Доступ к тестовой публикации запрещён.';
  end if;
  select * into v_gift from public.gifts where id = p_gift_id for update;
  if not found or v_gift.owner_id <> p_actor_id or v_gift.deleted_at is not null or
    v_gift.status <> 'draft' or v_gift.is_test then
    raise exception 'Тестовый черновик не найден.';
  end if;
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
    v_gift.scheduled_release_at <= v_now
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
  if v_gift.scheduled_release_at is not null then
    update public.gifts set is_test = true, status = 'awaiting_link'
      where id = v_gift.id returning * into v_gift;
  else
    update public.gifts set is_test = true, status = 'link_ready',
      public_token = encode(extensions.gen_random_bytes(32), 'hex'),
      link_available_at = v_now,
      expires_at = v_now + make_interval(days => (v_tariff.limits->>'link_days')::integer)
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
      and (g.is_test or exists (select 1 from public.payments p
        where p.gift_id = g.id and p.status = 'succeeded'))
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
    not (v_gift.is_test or exists (select 1 from public.payments p
      where p.gift_id = v_gift.id and p.status = 'succeeded')) then
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
    not (v_gift.is_test or exists (select 1 from public.payments p
      where p.gift_id = v_gift.id and p.status = 'succeeded')) or
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

revoke all on function public.publish_test_gift(uuid, uuid) from public, anon, authenticated;
grant execute on function public.publish_test_gift(uuid, uuid) to service_role;
