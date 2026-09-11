-- Real user accounts (Supabase Auth) + user-owned moodboards/datasets +
-- per-user interaction tracking. Supabase's own `auth.users` table already
-- lives in this SAME database (we connect straight to the Supabase Postgres
-- instance) -- no separate user store needed, just real FK references to it.

-- `public.profiles` mirrors the handful of auth.users fields the app
-- actually needs to display/join against. We don't query `auth.users`
-- directly from app code (that schema is Supabase-Auth-managed and its
-- shape isn't part of any stability contract) -- a synced public mirror is
-- the standard Supabase pattern.
-- Plain local Postgres has no Supabase-managed auth schema. Create the
-- smallest compatible table there so guest-mode migrations and foreign keys
-- remain valid; these statements are no-ops against a real Supabase database.
create schema if not exists auth;
create table if not exists auth.users (
  id         uuid primary key default gen_random_uuid(),
  email      text,
  created_at timestamptz not null default now()
);

create table if not exists profiles (
  id           uuid primary key references auth.users(id) on delete cascade,
  email        text,
  display_name text,
  created_at   timestamptz not null default now()
);

create or replace function handle_new_user() returns trigger as $$
begin
  insert into public.profiles (id, email, display_name)
  values (new.id, new.email, split_part(coalesce(new.email, ''), '@', 1))
  on conflict (id) do nothing;
  return new;
end
$$ language plpgsql security definer;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function handle_new_user();

-- Backfill: anyone already in auth.users before this migration ran.
insert into public.profiles (id, email, display_name)
select id, email, split_part(coalesce(email, ''), '@', 1) from auth.users
on conflict (id) do nothing;

-- `projects.user_id` already existed (nullable, "local/no-auth dev mode" --
-- see 0001_init.sql) but had no real FK and was never actually populated.
-- Real ownership starts now: add the FK (existing NULL rows -- anonymous
-- moodboards/datasets created before this migration -- are left as
-- legitimate "unowned/legacy" rows, not deleted or reassigned).
do $$
begin
  if not exists (
    select 1 from pg_constraint where conname = 'projects_user_id_fkey'
  ) then
    alter table projects
      add constraint projects_user_id_fkey foreign key (user_id) references auth.users(id) on delete cascade;
  end if;
end
$$;
create index if not exists projects_user_id_idx on projects(user_id);

-- Same real FK for asset_events.user_id (existed, unenforced, never populated).
do $$
begin
  if not exists (
    select 1 from pg_constraint where conname = 'asset_events_user_id_fkey'
  ) then
    alter table asset_events
      add constraint asset_events_user_id_fkey foreign key (user_id) references auth.users(id) on delete set null;
  end if;
end
$$;
create index if not exists asset_events_user_id_idx on asset_events(user_id);

-- One project per user is all this app's UI model needs today (a flat list
-- of "my moodboards" / "my datasets", no team/workspace concept yet) --
-- enforce it so "find or create my project" is a single indexed lookup,
-- not an ever-growing table of one-off "Default Project" rows (that WAS
-- happening: every old POST /moodboards with no project_id made a new one).
create unique index if not exists projects_one_per_user_idx on projects(user_id) where user_id is not null;
