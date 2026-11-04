-- Held AI: long-term user memory, one short summary per user of their recent conversations.
-- Written in the background when the user starts a new chat, and read together with the chat
-- on every later message, so replies remember other chats without reading all of them again.
-- Idempotent. Apply with
--   python agent/migrations/apply.py 006_user_memory.sql

create table if not exists public.user_memory (
  user_id text not null references public."User" (id) on delete cascade,
  summary text not null,
  updated_at timestamp with time zone not null default now(),
  constraint user_memory_pkey primary key (user_id)
);

-- Like the other tables: no access through Supabase's public API keys.
alter table public.user_memory enable row level security;
