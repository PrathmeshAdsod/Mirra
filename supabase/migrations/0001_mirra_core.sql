create extension if not exists pgcrypto;

create table public.profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  display_name text,
  created_at timestamptz not null default now()
);

create table public.brands (
  id uuid primary key default gen_random_uuid(),
  slug text not null unique,
  name text not null,
  is_public boolean not null default false,
  created_at timestamptz not null default now()
);

create table public.brand_members (
  brand_id uuid not null references public.brands(id) on delete cascade,
  user_id uuid not null references public.profiles(id) on delete cascade,
  role text not null default 'editor' check (role in ('owner','editor','viewer')),
  created_at timestamptz not null default now(),
  primary key (brand_id, user_id)
);

create table public.campaigns (
  id uuid primary key default gen_random_uuid(),
  brand_id uuid not null references public.brands(id) on delete cascade,
  name text not null,
  slug text,
  status text not null default 'draft' check (status in ('draft','preprocessing','inputs_pending','analyzing','review','published','failed')),
  source_path text,
  source_mime text,
  playback_path text,
  playback_mime text,
  source_bytes bigint check (source_bytes is null or source_bytes <= 47185920),
  source_checksum text,
  duration_seconds numeric,
  deterministic_metadata jsonb not null default '{}'::jsonb,
  processing_error text,
  brand_direction text,
  input_version integer not null default 1,
  published_manifest_id uuid,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (brand_id, slug)
);

create table public.products (
  id uuid primary key default gen_random_uuid(),
  brand_id uuid not null references public.brands(id) on delete cascade,
  name text not null,
  sku text,
  product_url text,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  unique (brand_id, sku)
);

create table public.campaign_products (
  campaign_id uuid not null references public.campaigns(id) on delete cascade,
  product_id uuid not null references public.products(id) on delete cascade,
  sort_order integer not null default 0,
  primary key (campaign_id, product_id)
);

create table public.look_reference_assets (
  id uuid primary key default gen_random_uuid(),
  brand_id uuid not null references public.brands(id) on delete cascade,
  product_id uuid references public.products(id) on delete set null,
  storage_path text not null,
  checksum text not null,
  mime_type text not null check (mime_type in ('image/jpeg','image/png')),
  width integer,
  height integer,
  validation_state text not null default 'pending' check (validation_state in ('pending','validated','rejected')),
  validation_result jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  unique (brand_id, product_id, checksum)
);

create table public.campaign_analyses (
  id uuid primary key default gen_random_uuid(),
  campaign_id uuid not null references public.campaigns(id) on delete cascade,
  cache_key text not null,
  model text not null,
  schema_version text not null,
  status text not null check (status in ('success','failed')),
  result jsonb,
  provider_interaction_id text,
  latency_ms integer,
  attempt_index smallint not null default 0 check (attempt_index between 0 and 1),
  created_at timestamptz not null default now()
);
create unique index campaign_analysis_attempt_unique on public.campaign_analyses(cache_key, attempt_index);

create table public.campaign_looks (
  id uuid primary key default gen_random_uuid(),
  campaign_id uuid not null references public.campaigns(id) on delete cascade,
  label text not null,
  description text,
  product_id uuid references public.products(id) on delete set null,
  reference_asset_id uuid references public.look_reference_assets(id) on delete restrict,
  garment_category text not null default 'auto' check (garment_category in ('outerwear','full_body','upper_body','lower_body','shoes','auto')),
  is_hero boolean not null default false,
  remix_allowed boolean not null default false,
  confidence numeric check (confidence between 0 and 1),
  poster_path text,
  sort_order integer not null,
  created_at timestamptz not null default now(),
  unique (campaign_id, sort_order)
);

create table public.campaign_segments (
  id uuid primary key default gen_random_uuid(),
  campaign_id uuid not null references public.campaigns(id) on delete cascade,
  look_id uuid not null references public.campaign_looks(id) on delete cascade,
  start_seconds numeric not null check (start_seconds >= 0),
  end_seconds numeric not null check (end_seconds > start_seconds),
  sort_order integer not null,
  unique (campaign_id, sort_order)
);

create table public.look_remix_options (
  id uuid primary key default gen_random_uuid(),
  look_id uuid not null references public.campaign_looks(id) on delete cascade,
  label text not null,
  reference_asset_id uuid not null references public.look_reference_assets(id) on delete restrict,
  reference_path text not null,
  garment_category text not null check (garment_category in ('outerwear','full_body','upper_body','lower_body','shoes','auto')),
  constraints jsonb not null default '{}'::jsonb,
  approved boolean not null default true,
  sort_order integer not null default 0,
  created_at timestamptz not null default now()
);

create table public.campaign_manifests (
  id uuid primary key default gen_random_uuid(),
  campaign_id uuid not null references public.campaigns(id) on delete cascade,
  version integer not null,
  content jsonb not null,
  content_checksum text not null,
  published boolean not null default false,
  published_at timestamptz,
  created_by uuid not null references public.profiles(id),
  created_at timestamptz not null default now(),
  unique (campaign_id, version),
  unique (campaign_id, content_checksum)
);
alter table public.campaigns add constraint campaigns_manifest_fk foreign key (published_manifest_id) references public.campaign_manifests(id) on delete set null;

create table public.shopper_photos (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.profiles(id) on delete cascade,
  storage_path text not null,
  checksum text not null,
  width integer not null,
  height integer not null,
  status text not null check (status in ('validated','rejected','deleted')),
  validation_result jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  deleted_at timestamptz,
  unique (user_id, checksum)
);

create table public.mirror_sessions (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.profiles(id) on delete cascade,
  manifest_id uuid not null references public.campaign_manifests(id) on delete restrict,
  shopper_photo_id uuid not null references public.shopper_photos(id) on delete restrict,
  status text not null default 'generating' check (status in ('generating','ready','partial','failed','deleted')),
  saved boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (user_id, manifest_id, shopper_photo_id)
);

create table public.youcam_requests (
  id uuid primary key default gen_random_uuid(),
  cache_key text not null unique,
  source_path text not null,
  source_checksum text not null,
  reference_path text not null,
  reference_checksum text not null,
  garment_category text not null check (garment_category in ('outerwear','full_body','upper_body','lower_body','shoes','auto')),
  provider_garment_category text not null check (provider_garment_category in ('full_body','upper_body','lower_body','shoes','auto')),
  provider_task_id text,
  provider_state text not null default 'queued' check (provider_state in ('queued','submitting','processing','success','failed','provider_unknown')),
  attempts integer not null default 0,
  next_poll_at timestamptz,
  result_path text,
  error jsonb,
  latency_ms integer,
  created_at timestamptz not null default now(),
  completed_at timestamptz
);

create table public.mirror_results (
  id uuid primary key default gen_random_uuid(),
  session_id uuid not null references public.mirror_sessions(id) on delete cascade,
  look_id uuid not null references public.campaign_looks(id) on delete restrict,
  remix_option_id uuid references public.look_remix_options(id) on delete set null,
  youcam_request_id uuid not null references public.youcam_requests(id) on delete restrict,
  normalized_constraints jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);
create unique index mirror_base_result_unique on public.mirror_results(session_id, look_id) where remix_option_id is null;
create unique index mirror_remix_result_unique on public.mirror_results(session_id, look_id, remix_option_id, youcam_request_id) where remix_option_id is not null;
create index mirror_results_request_idx on public.mirror_results(youcam_request_id);

create table public.provider_feasibility_runs (
  id uuid primary key default gen_random_uuid(),
  provider text not null,
  created_by uuid not null references public.profiles(id) on delete cascade,
  provider_task_id text not null,
  provider_state text not null check (provider_state in ('processing','success','failed','provider_unknown')),
  attempts integer not null default 0,
  next_poll_at timestamptz,
  request_metadata jsonb not null default '{}'::jsonb,
  result_path text,
  error jsonb,
  latency_ms integer,
  created_at timestamptz not null default now()
);

create table public.jobs (
  id uuid primary key default gen_random_uuid(),
  kind text not null,
  payload jsonb not null,
  priority integer not null default 50,
  status text not null default 'queued' check (status in ('queued','leased','complete','failed')),
  attempts integer not null default 0,
  max_attempts integer not null default 8,
  available_at timestamptz not null default now(),
  lease_owner text,
  lease_expires_at timestamptz,
  last_error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index jobs_claim_idx on public.jobs(status, available_at, priority desc, created_at) where status = 'queued';
create unique index gemini_campaign_active_job_unique on public.jobs ((payload->>'campaign_id'))
  where kind = 'gemini_campaign_analysis' and status in ('queued','leased');
create unique index youcam_request_active_job_unique on public.jobs ((payload->>'request_id'))
  where kind = 'youcam_request' and status in ('queued','leased');

create table public.usage_events (
  id uuid primary key default gen_random_uuid(),
  user_id uuid references public.profiles(id) on delete set null,
  event_name text not null,
  properties jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);
create index usage_events_daily_capacity_idx on public.usage_events(event_name, user_id, created_at);

create or replace function public.mirra_handle_new_user() returns trigger language plpgsql security definer set search_path = public as $$
begin
  insert into public.profiles(id, display_name) values (new.id, coalesce(new.raw_user_meta_data->>'display_name', split_part(new.email, '@', 1))) on conflict do nothing;
  return new;
end;
$$;
create trigger mirra_on_auth_user_created after insert on auth.users for each row execute function public.mirra_handle_new_user();

create or replace function public.is_brand_member(p_brand_id uuid, p_write boolean default false) returns boolean
language sql stable security definer set search_path = public as $$
  select exists(
    select 1 from public.brand_members m
    where m.brand_id = p_brand_id and m.user_id = auth.uid()
      and (not p_write or m.role in ('owner','editor'))
  );
$$;

create or replace function public.prevent_published_manifest_changes() returns trigger language plpgsql as $$
begin
  if old.published then raise exception 'Published campaign manifests are immutable'; end if;
  return case when tg_op = 'DELETE' then old else new end;
end;
$$;
create trigger campaign_manifest_immutable before update or delete on public.campaign_manifests for each row execute function public.prevent_published_manifest_changes();

create or replace function public.claim_jobs(p_worker_id text, p_limit integer default 4)
returns setof public.jobs language plpgsql security definer set search_path = public as $$
begin
  return query
  with candidates as (
    select id from public.jobs
    where (status = 'queued' and available_at <= now())
       or (status = 'leased' and lease_expires_at < now())
    order by priority desc, available_at asc, created_at asc
    for update skip locked
    limit greatest(1, least(p_limit, 10))
  )
  update public.jobs j set
    status = 'leased', lease_owner = p_worker_id,
    lease_expires_at = now() + case
      when j.kind = 'gemini_campaign_analysis' then interval '10 minutes'
      when j.kind = 'media_preprocess' then interval '5 minutes'
      else interval '2 minutes'
    end,
    attempts = attempts + 1, updated_at = now()
  from candidates c where j.id = c.id returning j.*;
end;
$$;

create or replace function public.finish_job(p_job_id uuid) returns void language sql security definer set search_path = public as $$
  update public.jobs set status = 'complete', lease_owner = null, lease_expires_at = null, updated_at = now() where id = p_job_id;
$$;

create or replace function public.reschedule_job(p_job_id uuid, p_available_at timestamptz, p_last_error text default null) returns void language sql security definer set search_path = public as $$
  update public.jobs set status = 'queued', available_at = p_available_at, last_error = p_last_error, lease_owner = null, lease_expires_at = null, updated_at = now() where id = p_job_id;
$$;

create or replace function public.fail_job(p_job_id uuid, p_last_error text) returns void language sql security definer set search_path = public as $$
  update public.jobs set status = 'failed', last_error = p_last_error, lease_owner = null, lease_expires_at = null, updated_at = now() where id = p_job_id;
$$;

create or replace function public.prioritize_mirror_result(p_result_id uuid, p_priority integer default 90) returns void language sql security definer set search_path = public as $$
  update public.jobs set priority = greatest(priority, p_priority), available_at = least(available_at, now()), updated_at = now()
  where status = 'queued' and kind = 'youcam_request' and payload->>'request_id' = (
    select r.youcam_request_id::text from public.mirror_results r where r.id = p_result_id
  );
$$;

create or replace function public.claim_youcam_submission_slot(p_request_id uuid, p_limit integer default 2)
returns boolean language plpgsql security definer set search_path = public as $$
declare
  v_state text;
  v_in_flight integer;
begin
  if p_limit < 1 then raise exception 'YouCam in-flight limit must be positive'; end if;
  perform pg_advisory_xact_lock(hashtextextended('mirra-youcam-submission-slot', 0));
  select provider_state into v_state from public.youcam_requests where id = p_request_id for update;
  if v_state is distinct from 'queued' then return false; end if;
  select count(*) into v_in_flight from public.youcam_requests
    where id <> p_request_id and provider_state in ('submitting', 'processing');
  if v_in_flight >= p_limit then return false; end if;
  update public.youcam_requests set provider_state = 'submitting', next_poll_at = null where id = p_request_id;
  return true;
end;
$$;

create or replace function public.refresh_mirror_session_status(p_session_id uuid) returns void language plpgsql security definer set search_path = public as $$
declare total_count integer; success_count integer; failed_count integer;
begin
  select count(*), count(*) filter (where q.provider_state = 'success'), count(*) filter (where q.provider_state in ('failed','provider_unknown'))
  into total_count, success_count, failed_count
  from public.mirror_results r
  join public.youcam_requests q on q.id = r.youcam_request_id
  where r.session_id = p_session_id and r.remix_option_id is null;
  update public.mirror_sessions set status = case
    when total_count > 0 and success_count = total_count then 'ready'
    when success_count > 0 and success_count + failed_count = total_count then 'partial'
    when total_count > 0 and failed_count = total_count then 'failed'
    else 'generating' end,
    updated_at = now() where id = p_session_id;
end;
$$;

create or replace function public.reserve_daily_capacity(
  p_event_name text,
  p_limit integer,
  p_reservation_keys text[],
  p_user_id uuid default null
) returns integer language plpgsql security definer set search_path = public as $$
declare
  v_day_start timestamptz := date_trunc('day', now() at time zone 'UTC') at time zone 'UTC';
  v_used integer;
  v_missing text[];
begin
  if p_limit < 1 then raise exception 'Provider capacity limit must be positive'; end if;
  perform pg_advisory_xact_lock(hashtextextended('mirra-capacity:' || p_event_name || ':' || coalesce(p_user_id::text, 'global'), 0));

  select coalesce(array_agg(k order by k), '{}'::text[]) into v_missing
  from (
    select distinct btrim(key) as k
    from unnest(coalesce(p_reservation_keys, '{}'::text[])) as candidate(key)
    where btrim(key) <> ''
      and not exists (
        select 1 from public.usage_events e
        where e.event_name = p_event_name
          and e.user_id is not distinct from p_user_id
          and e.created_at >= v_day_start
          and e.properties->>'reservation_key' = btrim(key)
      )
  ) missing;

  select count(*) into v_used from public.usage_events e
  where e.event_name = p_event_name
    and e.user_id is not distinct from p_user_id
    and e.created_at >= v_day_start;

  if v_used + cardinality(v_missing) > p_limit then return -1; end if;
  insert into public.usage_events(user_id, event_name, properties)
    select p_user_id, p_event_name, jsonb_build_object('reservation_key', key)
    from unnest(v_missing) as reserved(key);
  return cardinality(v_missing);
end;
$$;

alter table public.profiles enable row level security;
alter table public.brands enable row level security;
alter table public.brand_members enable row level security;
alter table public.campaigns enable row level security;
alter table public.products enable row level security;
alter table public.campaign_products enable row level security;
alter table public.look_reference_assets enable row level security;
alter table public.campaign_analyses enable row level security;
alter table public.campaign_looks enable row level security;
alter table public.campaign_segments enable row level security;
alter table public.look_remix_options enable row level security;
alter table public.campaign_manifests enable row level security;
alter table public.shopper_photos enable row level security;
alter table public.mirror_sessions enable row level security;
alter table public.youcam_requests enable row level security;
alter table public.mirror_results enable row level security;
alter table public.provider_feasibility_runs enable row level security;
alter table public.jobs enable row level security;
alter table public.usage_events enable row level security;

create policy "profiles self" on public.profiles for all using (id = auth.uid()) with check (id = auth.uid());
create policy "public brands readable" on public.brands for select using (is_public or public.is_brand_member(id));
create policy "members see membership" on public.brand_members for select using (user_id = auth.uid() or public.is_brand_member(brand_id));
create policy "campaigns readable" on public.campaigns for select using (status = 'published' or public.is_brand_member(brand_id));
create policy "brand members manage campaigns" on public.campaigns for all using (public.is_brand_member(brand_id, true)) with check (public.is_brand_member(brand_id, true));
create policy "own shopper photos" on public.shopper_photos for all using (user_id = auth.uid()) with check (user_id = auth.uid());
create policy "own mirror sessions" on public.mirror_sessions for all using (user_id = auth.uid()) with check (user_id = auth.uid());
create policy "own mirror results" on public.mirror_results for select using (exists(select 1 from public.mirror_sessions s where s.id = session_id and s.user_id = auth.uid()));
create policy "own feasibility runs" on public.provider_feasibility_runs for select using (created_by = auth.uid());

insert into storage.buckets(id, name, public, file_size_limit, allowed_mime_types) values
  ('campaign-source','campaign-source',false,47185920,array['video/mp4','video/quicktime','video/webm','video/x-matroska']),
  ('look-references','look-references',false,10485760,array['image/jpeg','image/png']),
  ('campaign-frames','campaign-frames',false,5242880,array['image/jpeg']),
  ('mirror-private','mirror-private',false,10485760,array['image/jpeg','image/png']),
  ('mirror-results','mirror-results',false,10485760,array['image/jpeg','image/png'])
on conflict (id) do update set file_size_limit = excluded.file_size_limit, allowed_mime_types = excluded.allowed_mime_types;

revoke all on function public.claim_jobs(text, integer) from public, anon, authenticated;
revoke all on function public.finish_job(uuid) from public, anon, authenticated;
revoke all on function public.reschedule_job(uuid, timestamptz, text) from public, anon, authenticated;
revoke all on function public.fail_job(uuid, text) from public, anon, authenticated;
revoke all on function public.refresh_mirror_session_status(uuid) from public, anon, authenticated;
revoke all on function public.prioritize_mirror_result(uuid, integer) from public, anon, authenticated;
revoke all on function public.claim_youcam_submission_slot(uuid, integer) from public, anon, authenticated;
revoke all on function public.reserve_daily_capacity(text, integer, text[], uuid) from public, anon, authenticated;

grant execute on function public.claim_jobs(text, integer) to service_role;
grant execute on function public.finish_job(uuid) to service_role;
grant execute on function public.reschedule_job(uuid, timestamptz, text) to service_role;
grant execute on function public.fail_job(uuid, text) to service_role;
grant execute on function public.refresh_mirror_session_status(uuid) to service_role;
grant execute on function public.prioritize_mirror_result(uuid, integer) to service_role;
grant execute on function public.claim_youcam_submission_slot(uuid, integer) to service_role;
grant execute on function public.reserve_daily_capacity(text, integer, text[], uuid) to service_role;
