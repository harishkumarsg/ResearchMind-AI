-- ---------------------------------------------------------------------
-- 0004: usage_counters — durable per-user daily usage quotas (mechanism B).
--
-- NOT YET APPLIED. Run this as the `postgres` role (Supabase SQL editor
-- or a postgres connection) BEFORE deploying the code that reads it:
-- an endpoint that cannot reach its counter fails closed by design.
--
-- One row per (owner_id, day, metric). The composite primary key is the
-- only index required — every read and write addresses a single row by
-- the full key. `day` is a UTC date supplied by the application, never
-- now()::date, so the rollover point cannot drift with server settings
-- and the offline SQLite tests exercise identical semantics.
--
-- The application NEVER deletes counters. Removal happens only by
-- cascade when the auth.users row goes, which is performed with the
-- referencing table's privileges, so the app role needs no DELETE.
--
-- ---------------------------------------------------------------------
-- PRIVILEGE MODEL — verified against the live database, not assumed.
--
-- Checked with information_schema.role_table_grants, pg_class.relacl,
-- pg_auth_members and pg_default_acl:
--
--   * papers / reports / chat_sessions / chat_messages each carry an
--     EXPLICIT per-table grant:  researchmind_app=arwd/postgres
--     (INSERT, SELECT, UPDATE, DELETE, granted by postgres).
--   * researchmind_app has NO role memberships and is NOINHERIT, so it
--     inherits nothing from anon / authenticated / service_role.
--   * DEFAULT PRIVILEGES for postgres in schema public grant to
--     postgres, anon, authenticated and service_role — but NOT to
--     researchmind_app.
--
-- Therefore a new table receives NO automatic grant for the app role,
-- and this migration MUST grant explicitly, exactly as the four existing
-- tables were granted. Least privilege: SELECT, INSERT, UPDATE only —
-- no DELETE, per the cascade note above.
--
-- researchmind_app already holds USAGE on schema public
-- (public schema ACL: researchmind_app=U/pg_database_owner).
-- ---------------------------------------------------------------------

create table if not exists usage_counters (
    owner_id    uuid        not null references auth.users(id) on delete cascade,
    day         date        not null,
    metric      text        not null,
    count       integer     not null default 0 check (count >= 0),
    updated_at  timestamptz not null default now(),
    primary key (owner_id, day, metric)
);

-- ---------------------------------------------------------------------
-- Row Level Security — the same posture as the four C1 tables, which was
-- verified live: tables owned by `postgres`, the application role is
-- NOBYPASSRLS and is not the owner, FORCE ROW LEVEL SECURITY is set, and
-- the per-transaction request.jwt.claims binding in app/db/session.py
-- makes auth.uid() resolve to the verified JWT sub.
--
-- Both USING and WITH CHECK are required here. USING governs what an
-- owner can read and update; WITH CHECK governs what it may write, and
-- without it a caller could INSERT a counter row attributed to another
-- owner_id. This table is written by an UPSERT, which is both.
-- ---------------------------------------------------------------------
alter table usage_counters enable row level security;
alter table usage_counters force row level security;

drop policy if exists usage_counters_owner_only on usage_counters;
create policy usage_counters_owner_only on usage_counters
    using (owner_id = auth.uid())
    with check (owner_id = auth.uid());

grant select, insert, update on usage_counters to researchmind_app;
