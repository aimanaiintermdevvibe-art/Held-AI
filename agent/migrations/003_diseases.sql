-- Held AI: the disease catalogue users choose their diagnosed conditions from.
-- Mirrors Held's GET /diseases (ids are Held's disease UUIDs, the same ids that appear in a
-- user's profile `diseases`). Idempotent. Apply with
--   python agent/migrations/apply.py 003_diseases.sql
-- then fill it with
--   python agent/scripts/seed_diseases.py

create table if not exists public."Disease" (
  id text not null,
  name text not null,
  abbreviation text null,
  "imageUrl" text null,
  "createdAt" timestamp without time zone not null default CURRENT_TIMESTAMP,
  "updatedAt" timestamp without time zone not null default CURRENT_TIMESTAMP,
  constraint "Disease_pkey" primary key (id)
);

create unique index if not exists "Disease_name_key" on public."Disease" using btree (name);

-- Like the other tables: no access through Supabase's public API keys.
alter table public."Disease" enable row level security;
