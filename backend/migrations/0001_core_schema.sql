-- Phase 1.5 core schema: papers, reports, chat_sessions.
--
-- Run this in the Supabase SQL editor, or via
--   psql "$DATABASE_URL" -f 0001_core_schema.sql
-- against the Supabase Postgres instance once the project exists.
-- NOT executed by the application. Not run yet as of Phase 1.5 planning.
--
-- References auth.users, which Supabase creates and manages automatically.
-- Do not create, modify, or drop that table from here.

create extension if not exists "pgcrypto";

create table if not exists papers (
    id                uuid primary key,
    owner_id          uuid not null references auth.users(id) on delete cascade,
    title             text not null,
    content_hash      text not null,
    storage_path      text not null,
    file_size_bytes   bigint not null,
    status            text not null default 'uploading'
                        check (status in ('uploading', 'uploaded', 'indexing', 'indexed', 'failed', 'deleting')),
    status_detail     text,
    created_at        timestamptz not null default now(),
    updated_at        timestamptz not null default now(),
    unique (owner_id, content_hash)
);

create index if not exists papers_owner_id_idx on papers (owner_id);

create table if not exists reports (
    id                uuid primary key default gen_random_uuid(),
    owner_id          uuid not null references auth.users(id) on delete cascade,
    query             text not null,
    report_markdown   text not null,
    citations         jsonb,
    created_at        timestamptz not null default now()
);

create index if not exists reports_owner_id_idx on reports (owner_id);

create table if not exists chat_sessions (
    id                  uuid primary key default gen_random_uuid(),
    owner_id            uuid not null references auth.users(id) on delete cascade,
    current_topic       text,
    current_paper_id    uuid references papers(id) on delete set null,
    updated_at          timestamptz not null default now()
);

create index if not exists chat_sessions_owner_id_idx on chat_sessions (owner_id);

-- ---------------------------------------------------------------------
-- Defense-in-depth: Postgres Row Level Security on these tables.
--
-- IMPORTANT CAVEAT: this only actively enforces anything if the backend's
-- database connection carries the authenticated user's role/claims on a
-- per-request basis (e.g. via `set_config('request.jwt.claims', ...)`,
-- or by routing app-table reads through Supabase's PostgREST layer
-- instead of a raw SQLAlchemy connection).
--
-- As currently designed, the FastAPI backend connects with a single
-- DATABASE_URL (effectively a service-role-equivalent connection) via
-- SQLAlchemy, which does NOT automatically assume the calling user's
-- role. In that configuration, these policies exist but are NOT the
-- active enforcement mechanism. The mandatory, load-bearing check is
-- the explicit `WHERE owner_id = :owner_id` applied in every backend
-- query, using the owner_id derived from the verified JWT
-- (backend/app/core/auth.py). Enabling RLS here is still worthwhile as
-- a backstop against a future connection-handling change, and it costs
-- nothing to have in place now.
-- ---------------------------------------------------------------------

alter table papers enable row level security;
alter table reports enable row level security;
alter table chat_sessions enable row level security;

create policy papers_owner_only on papers
    using (owner_id = auth.uid());

create policy reports_owner_only on reports
    using (owner_id = auth.uid());

create policy chat_sessions_owner_only on chat_sessions
    using (owner_id = auth.uid());
