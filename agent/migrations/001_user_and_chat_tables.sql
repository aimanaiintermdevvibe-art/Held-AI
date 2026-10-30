-- Held AI: User + chat memory tables for Supabase.
-- Mirrors the live Held database. Every statement is idempotent, so running it
-- against a database that already has these objects is a no-op; on a fresh
-- database it creates everything the agent needs.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'AuthProvider' AND typnamespace = 'public'::regnamespace) THEN
    CREATE TYPE public."AuthProvider" AS ENUM ('EMAIL', 'GOOGLE', 'APPLE', 'FACEBOOK', 'GITHUB');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'OAuthProvider' AND typnamespace = 'public'::regnamespace) THEN
    CREATE TYPE public."OAuthProvider" AS ENUM ('GOOGLE', 'APPLE', 'FACEBOOK', 'GITHUB');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'UserRole' AND typnamespace = 'public'::regnamespace) THEN
    CREATE TYPE public."UserRole" AS ENUM ('USER', 'ADMIN', 'PATIENT', 'CAREGIVER');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'RelationType' AND typnamespace = 'public'::regnamespace) THEN
    CREATE TYPE public."RelationType" AS ENUM ('SELF', 'PARENT', 'CHILD', 'SPOUSE', 'SIBLING', 'FRIEND', 'OTHER');
  END IF;
END
$$;

-- 1. Users (identity source for the agent's bearer-token authentication)
create table if not exists public."User" (
  id text not null,
  email text not null,
  password text null,
  "fullName" text not null,
  "authProvider" public."AuthProvider" not null default 'EMAIL'::"AuthProvider",
  "providerId" text null,
  "emailVerified" boolean not null default false,
  "onboardingComplete" boolean not null default false,
  role public."UserRole" null,
  dob timestamp without time zone null,
  username text null,
  "pseudoName" text null,
  relation public."RelationType" null,
  "relationOther" text null,
  "createdAt" timestamp without time zone not null default CURRENT_TIMESTAMP,
  "updatedAt" timestamp without time zone not null,
  provider public."OAuthProvider" null,
  "avatarKey" text null,
  constraint "User_pkey" primary key (id),
  constraint "User_provider_pair_check" check (((provider is null) = ("providerId" is null)))
) TABLESPACE pg_default;

create unique index if not exists "User_email_key" on public."User" using btree (email) TABLESPACE pg_default;
create unique index if not exists "User_username_key" on public."User" using btree (username) TABLESPACE pg_default;
create unique index if not exists "User_provider_providerId_key" on public."User" using btree (provider, "providerId") TABLESPACE pg_default;

-- 2. Chat sessions: one per conversation. The rolling summary is kept in
--    session_summary (and metadata->'memory' with its counters).
create table if not exists public.chat_sessions (
  id uuid not null default gen_random_uuid(),
  user_id text null references public."User" (id) on delete set null,
  title text not null default 'New Conversation',
  status text not null default 'active',
  metadata jsonb null default '{}'::jsonb,
  created_at timestamp with time zone null default now(),
  updated_at timestamp with time zone null default now(),
  session_summary text null,
  last_summarized_message_id uuid null,
  total_message_count integer not null default 0,
  constraint chat_sessions_pkey primary key (id)
);

create index if not exists idx_chat_sessions_user_id on public.chat_sessions using btree (user_id);
create index if not exists idx_chat_sessions_created_at on public.chat_sessions using btree (created_at desc);

-- 3. Chat messages: full history the agent replays as context.
create table if not exists public.chat_messages (
  id uuid not null default gen_random_uuid(),
  session_id uuid not null references public.chat_sessions (id) on delete cascade,
  role text not null,
  content text not null,
  metadata jsonb null default '{}'::jsonb,
  created_at timestamp with time zone null default now(),
  constraint chat_messages_pkey primary key (id),
  constraint chat_messages_role_check check (role = any (array['user'::text, 'assistant'::text, 'system'::text]))
);

create index if not exists idx_chat_messages_session_id_created_at
  on public.chat_messages using btree (session_id, created_at);
