-- Phase D2 migration: durable chat persistence.
--
-- Additive and non-destructive. Creates one new table, two indexes, one
-- policy, and adds a UNIQUE constraint to an EMPTY existing table.
-- No row is updated or deleted; no column is dropped or retyped.
-- Touches Postgres only — Qdrant, Supabase Storage, and the indexed
-- vector data are entirely unaffected.
--
-- Run in the Supabase SQL editor, or:
--   psql "$DATABASE_URL" -f 0003_chat_messages.sql
-- NOT executed by the application.
--
-- Preconditions verified read-only before authoring (2026-09-13):
--   chat_sessions: 0 rows, 0 duplicate owner_ids  -> UNIQUE is safe
--   chat_messages / chat_sessions_owner_id_key / both index names /
--   the policy name: all free, no collisions
--   pgcrypto installed -> gen_random_uuid() available

-- ---------------------------------------------------------------------
-- 1. One active chat session per user.
--
-- D2 persists a single conversation per owner. Without this constraint
-- two concurrent requests could each insert a session row for the same
-- owner, and ON CONFLICT (owner_id) DO UPDATE would be unavailable.
-- Verified safe: chat_sessions currently holds 0 rows and 0 duplicate
-- owner_ids, so this cannot fail or destroy anything.
-- Guarded so re-running the migration is a no-op rather than an error.
-- ---------------------------------------------------------------------
do $$
begin
    if not exists (
        select 1 from pg_constraint where conname = 'chat_sessions_owner_id_key'
    ) then
        alter table chat_sessions
            add constraint chat_sessions_owner_id_key unique (owner_id);
    end if;
end $$;

-- ---------------------------------------------------------------------
-- 2. Chat messages.
--
-- chat_sessions stores only the session pointer (current_topic,
-- current_paper_id); there is nowhere for the turn-by-turn history that
-- currently lives in app.memory's in-process dict. This table is that
-- home.
--
-- owner_id is deliberately DENORMALISED alongside session_id. Every
-- query can then filter by owner directly with no join — matching the
-- explicit `WHERE owner_id = :owner_id` pattern used everywhere else —
-- and the RLS policy below can be written against this table alone.
-- ---------------------------------------------------------------------
create table if not exists chat_messages (
    id          uuid primary key default gen_random_uuid(),
    session_id  uuid not null references chat_sessions(id) on delete cascade,
    owner_id    uuid not null references auth.users(id)    on delete cascade,
    role        text not null check (role in ('user', 'assistant')),
    content     text not null,
    created_at  timestamptz not null default now()
);

-- History is always read as "this session's turns, oldest first".
create index if not exists chat_messages_session_created_idx
    on chat_messages (session_id, created_at);

-- Supports the owner-scoped filter applied to every read.
create index if not exists chat_messages_owner_id_idx
    on chat_messages (owner_id);

-- ---------------------------------------------------------------------
-- 3. RLS: enabled, with an owner-only policy — matching the schema
--    posture of papers, reports, and chat_sessions.
--
-- IMPORTANT: enabling RLS here does NOT activate enforcement. As on the
-- other three tables it is inert, because the application connects as a
-- role that both owns these tables and carries BYPASSRLS, and
-- FORCE ROW LEVEL SECURITY is not set. This statement exists so the new
-- table has the same posture as its siblings, not because it protects
-- anything today.
--
-- FORCE ROW LEVEL SECURITY is deliberately NOT set here. Real
-- enforcement is deferred to the C1 milestone, which must land four
-- changes together: a non-owner role without BYPASSRLS, FORCE ROW LEVEL
-- SECURITY, per-request JWT claims, and SET LOCAL discipline so pooled
-- connections cannot leak one request's claims into the next.
--
-- Until then the load-bearing check remains the explicit
-- `WHERE owner_id = :owner_id` applied in every backend query, using the
-- owner_id derived from the verified JWT (backend/app/core/auth.py).
-- ---------------------------------------------------------------------
alter table chat_messages enable row level security;

drop policy if exists chat_messages_owner_only on chat_messages;
create policy chat_messages_owner_only on chat_messages
    using (owner_id = auth.uid());
