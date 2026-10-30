-- Held AI: background ingestion for documents of any length.
-- An upload stores every chunk straight away with an empty embedding; a background worker
-- fills the embeddings in batches and records progress, and resumes after a restart.
-- Idempotent. Apply with
--   python agent/migrations/apply.py 004_document_ingestion_progress.sql

alter table public.documents add column if not exists status text not null default 'ready';
alter table public.documents add column if not exists total_chunks integer null;
alter table public.documents add column if not exists error text null;
alter table public.documents add column if not exists updated_at timestamp with time zone not null default now();

-- paused: Gemini's embedding quota is used up; the worker continues when it resets.
alter table public.documents drop constraint if exists documents_status_check;
alter table public.documents add constraint documents_status_check
  check (status in ('queued', 'processing', 'paused', 'ready', 'failed'));

-- Documents stored before this migration were fully embedded when uploaded.
update public.documents set total_chunks = chunk_count where total_chunks is null;

-- chunk_count now counts embedded chunks; total_chunks counts all of them.
alter table public.document_chunks alter column embedding drop not null;

-- The worker picks up unfinished documents on startup.
drop index if exists public.idx_documents_unfinished;
create index if not exists idx_documents_unfinished_v2
  on public.documents using btree (created_at) where status in ('queued', 'processing', 'paused');

-- The worker fetches the next chunks still waiting for an embedding.
create index if not exists idx_document_chunks_unembedded
  on public.document_chunks using btree (document_id, chunk_index) where embedding is null;
