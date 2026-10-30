-- Held AI: uploaded documents and their embedded chunks for retrieval (RAG), using pgvector.
-- Idempotent: running it again is a no-op. Apply with
--   python agent/migrations/apply.py 002_documents_pgvector.sql
-- or paste it into the Supabase SQL editor.

-- pgvector lives in Supabase's "extensions" schema, which is on the default search_path.
create extension if not exists vector with schema extensions;

-- 1. One row per uploaded file, owned by a Held user.
create table if not exists public.documents (
  id uuid not null default gen_random_uuid(),
  user_id text not null references public."User" (id) on delete cascade,
  filename text not null,
  content_type text null,
  size_bytes integer not null,
  sha256 text not null,                 -- the same file is stored once per user
  content text not null,                -- extracted text, kept so chunks can be rebuilt
  chunk_count integer not null default 0,
  embedding_model text not null,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamp with time zone not null default now(),
  constraint documents_pkey primary key (id),
  constraint documents_user_sha256_key unique (user_id, sha256)
);

create index if not exists idx_documents_user_id_created_at
  on public.documents using btree (user_id, created_at desc);

-- 2. Retrieval units: a passage of a document with its embedding.
--    768 dimensions = gemini-embedding-001 with output_dimensionality 768 (HNSW supports up to 2000).
create table if not exists public.document_chunks (
  id uuid not null default gen_random_uuid(),
  document_id uuid not null references public.documents (id) on delete cascade,
  user_id text not null,                -- copied from documents for owner-filtered search
  chunk_index integer not null,
  content text not null,
  metadata jsonb not null default '{}'::jsonb,   -- e.g. {"page": 3}
  embedding extensions.vector(768) not null,
  created_at timestamp with time zone not null default now(),
  constraint document_chunks_pkey primary key (id),
  constraint document_chunks_document_index_key unique (document_id, chunk_index)
);

create index if not exists idx_document_chunks_user_id
  on public.document_chunks using btree (user_id);

-- Approximate nearest-neighbour search by cosine distance (the <=> operator).
create index if not exists idx_document_chunks_embedding
  on public.document_chunks using hnsw (embedding extensions.vector_cosine_ops);

-- Like the other tables: no access through Supabase's public API keys. The agent connects
-- as the table owner, which row-level security does not restrict.
alter table public.documents enable row level security;
alter table public.document_chunks enable row level security;
