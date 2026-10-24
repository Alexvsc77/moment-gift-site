-- Moment: private gift data, owner-only reads, and server-only writes.
-- Apply to a dedicated Supabase project. Payment state is never client-writable.

create extension if not exists pgcrypto with schema extensions;

create table public.tariffs (
  slug text primary key,
  name text not null,
  price_rub integer not null check (price_rub > 0),
  limits jsonb not null default '{}'::jsonb,
  is_active boolean not null default true,
  sort_order smallint not null default 0,
  updated_at timestamptz not null default now(),
  constraint tariffs_slug_format check (slug ~ '^[a-z][a-z0-9_-]{1,39}$')
);

create table public.profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  display_name text not null default '',
  contact_email text not null default '',
  telegram_id text unique,
  vk_id text unique,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table public.gifts (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid not null references auth.users(id) on delete restrict,
  tariff_slug text not null references public.tariffs(slug) on delete restrict,
  status text not null default 'draft' check (status in (
    'draft', 'awaiting_payment', 'awaiting_link', 'link_ready',
    'opened', 'answered', 'expired', 'cancelled', 'refunded'
  )),
  recipient_name text not null default '' check (char_length(recipient_name) <= 50),
  title text not null default '' check (char_length(title) <= 70),
  message text not null default '' check (char_length(message) <= 600),
  sender_name text not null default '' check (char_length(sender_name) <= 50),
  occasion text not null default '' check (char_length(occasion) <= 50),
  contact_email text not null default '',
  template text not null default 'petal' check (template in ('petal', 'night', 'sun', 'aurora', 'bloom')),
  music_track text not null default '' check (music_track in ('', 'calm', 'warm', 'bright')),
  custom_slug text,
  public_token text unique,
  scheduled_release_at timestamptz,
  link_available_at timestamptz,
  expires_at timestamptz,
  opened_at timestamptz,
  open_notification_sent_at timestamptz,
  answered_at timestamptz,
  deleted_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint gifts_slug_format check (custom_slug is null or (
    char_length(custom_slug) between 1 and 60 and custom_slug ~ '^[[:alnum:]_-]+$'
  )),
  constraint gifts_token_format check (public_token is null or public_token ~ '^[a-f0-9]{64}$'),
  constraint gifts_expiry_after_release check (
    expires_at is null or (link_available_at is not null and expires_at > link_available_at)
  )
);

create unique index gifts_custom_slug_unique on public.gifts (lower(custom_slug))
  where custom_slug is not null;
create index gifts_owner_status_idx on public.gifts (owner_id, status, created_at desc);
create index gifts_scheduled_release_idx on public.gifts (scheduled_release_at)
  where status = 'awaiting_link';
create index gifts_expiry_idx on public.gifts (expires_at)
  where expires_at is not null;

create table public.gift_media (
  id uuid primary key default gen_random_uuid(),
  gift_id uuid not null references public.gifts(id) on delete cascade,
  storage_path text not null unique,
  kind text not null check (kind in ('photo', 'video')),
  size_bytes bigint not null check (size_bytes > 0),
  mime_type text not null check (mime_type in ('image/jpeg', 'image/png', 'image/webp', 'video/mp4')),
  sort_order smallint not null default 0,
  created_at timestamptz not null default now()
);
create index gift_media_gift_idx on public.gift_media (gift_id, sort_order, created_at);

create table public.payments (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid not null references auth.users(id) on delete restrict,
  gift_id uuid not null references public.gifts(id) on delete restrict,
  tariff_slug text not null references public.tariffs(slug) on delete restrict,
  amount_rub integer not null check (amount_rub > 0),
  currency text not null default 'RUB' check (currency = 'RUB'),
  status text not null default 'pending' check (status in ('pending', 'succeeded', 'cancelled', 'refunded')),
  provider_payment_id text unique,
  idempotence_key uuid not null default gen_random_uuid() unique,
  provider_refund_id text unique,
  refund_idempotence_key uuid not null default gen_random_uuid() unique,
  refund_requested_at timestamptz,
  paid_at timestamptz,
  refunded_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create unique index payments_one_completed_per_gift on public.payments (gift_id)
  where status in ('succeeded', 'refunded');
create index payments_owner_created_idx on public.payments (owner_id, created_at desc);

create table public.recipient_responses (
  gift_id uuid primary key references public.gifts(id) on delete cascade,
  choice text not null check (choice in ('yes', 'think')),
  responded_at timestamptz not null default now()
);

create or replace function public.set_updated_at()
returns trigger language plpgsql set search_path = '' as $$
begin
  new.updated_at := now();
  return new;
end;
$$;
create trigger tariffs_updated_at before update on public.tariffs
  for each row execute function public.set_updated_at();
create trigger profiles_updated_at before update on public.profiles
  for each row execute function public.set_updated_at();
create trigger gifts_updated_at before update on public.gifts
  for each row execute function public.set_updated_at();
create trigger payments_updated_at before update on public.payments
  for each row execute function public.set_updated_at();

alter table public.tariffs enable row level security;
alter table public.profiles enable row level security;
alter table public.gifts enable row level security;
alter table public.gift_media enable row level security;
alter table public.payments enable row level security;
alter table public.recipient_responses enable row level security;

revoke all on public.tariffs, public.profiles, public.gifts,
  public.gift_media, public.payments, public.recipient_responses
  from anon, authenticated;
revoke all on function public.set_updated_at() from public, anon, authenticated;

grant select on public.tariffs to anon, authenticated;
grant select on public.profiles, public.gifts, public.gift_media,
  public.payments, public.recipient_responses to authenticated;

create policy tariffs_read_active on public.tariffs for select
  to anon, authenticated using (is_active);
create policy profiles_read_own on public.profiles for select
  to authenticated using (id = (select auth.uid()));
create policy gifts_read_own on public.gifts for select
  to authenticated using (owner_id = (select auth.uid()));
create policy gift_media_read_own on public.gift_media for select
  to authenticated using (exists (
    select 1 from public.gifts g
    where g.id = gift_id and g.owner_id = (select auth.uid())
  ));
create policy payments_read_own on public.payments for select
  to authenticated using (owner_id = (select auth.uid()));
create policy responses_read_own on public.recipient_responses for select
  to authenticated using (exists (
    select 1 from public.gifts g
    where g.id = gift_id and g.owner_id = (select auth.uid())
  ));

insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values (
  'gift-media', 'gift-media', false, 52428800,
  array['image/jpeg', 'image/png', 'image/webp', 'video/mp4']
)
on conflict (id) do nothing;

insert into public.tariffs (slug, name, price_rub, sort_order, limits) values
  ('lite', 'Лайт', 200, 1, '{
    "max_photos":3,"max_photo_bytes":5242880,"link_days":7,
    "allowed_templates":["petal"],"allow_music":false,"allow_answers":false,
    "allow_video":false,"allow_scheduled_release":false,"notify_on_open":false,
    "service_signature":true
  }'::jsonb),
  ('standard', 'Стандарт', 350, 2, '{
    "max_photos":10,"max_photo_bytes":5242880,"link_days":30,
    "allowed_templates":["petal","night","sun"],"allow_music":true,
    "allow_answers":true,"allow_video":false,"allow_scheduled_release":false,
    "notify_on_open":false,"service_signature":false
  }'::jsonb),
  ('premium', 'Премиум', 450, 3, '{
    "max_photos":10,"max_photo_bytes":5242880,"max_video_bytes":52428800,
    "link_days":30,"allowed_templates":["petal","night","sun","aurora","bloom"],
    "allow_music":true,"allow_answers":true,"allow_video":true,
    "allow_scheduled_release":true,"notify_on_open":true,"service_signature":false
  }'::jsonb);
