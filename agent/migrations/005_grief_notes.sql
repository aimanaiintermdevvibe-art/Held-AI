-- Held AI: grief notes (the Held app's daily grief journal) stored per user, plus the mood catalogue.
-- Held's API (GET/POST /notes, GET/PATCH /notes/{date}) stays the source of truth: the agent
-- writes through it and keeps a copy of every note it creates, edits or reads here, together
-- with the workbook exercise the note answers (which only the agent knows about).
-- Idempotent. Apply with
--   python agent/migrations/apply.py 005_grief_notes.sql
-- then fill the mood catalogue with
--   python agent/scripts/seed_moods.py

-- 1. Moods a note can carry (Held's GET /moods; ids are Held's).
create table if not exists public."Mood" (
  id text not null,
  code text not null,
  title text not null,
  "imageUrl" text null,
  "createdAt" timestamp without time zone not null default CURRENT_TIMESTAMP,
  "updatedAt" timestamp without time zone not null default CURRENT_TIMESTAMP,
  constraint "Mood_pkey" primary key (id)
);

create unique index if not exists "Mood_code_key" on public."Mood" using btree (code);

-- 2. One note per user per calendar date, like Held.
create table if not exists public.grief_notes (
  id text not null,                          -- Held's note id
  user_id text not null references public."User" (id) on delete cascade,
  note_date date not null,
  content text not null,
  mood_id text null,
  mood jsonb null,                           -- {id, code, title, imageUrl} as Held returned it
  exercise_key text null,                    -- workbook exercise this note answers (agent only)
  held_created_at timestamp with time zone null,
  held_updated_at timestamp with time zone null,
  synced_at timestamp with time zone not null default now(),
  created_at timestamp with time zone not null default now(),
  constraint grief_notes_pkey primary key (id),
  constraint grief_notes_user_date_key unique (user_id, note_date)
);

create index if not exists idx_grief_notes_user_date
  on public.grief_notes using btree (user_id, note_date desc);

-- Like the other tables: no access through Supabase's public API keys.
alter table public."Mood" enable row level security;
alter table public.grief_notes enable row level security;
