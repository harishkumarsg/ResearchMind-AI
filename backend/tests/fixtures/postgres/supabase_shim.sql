-- tests/fixtures/postgres/supabase_shim.sql
--
-- The Supabase-provided objects the repository's migrations reference,
-- recreated for a DISPOSABLE local PostgreSQL 17 used only by
-- tests/test_paper_lifecycle_postgres.py. Never apply this to a Supabase
-- project: Supabase already owns these objects.
--
-- Apply as the bootstrap superuser, BEFORE migrations/bootstrap/app_role.sql
-- and migrations 0001, 0003-0007 (0002 needs Supabase's storage schema and
-- is not part of these tests).
--
-- Production fidelity. Production reports
--   has_schema_privilege('researchmind_app', 'auth', 'USAGE') = false
-- and this file reproduces that: schema auth gets no grant at all, so the
-- app role holds only what the migrations give it — 0007's EXECUTE on
-- auth.uid(). PUBLIC's default EXECUTE on the function is revoked so that
-- grant is the one actually being exercised. Nothing here or in the test
-- setup makes the app role more privileged than production.

create schema if not exists auth;

-- Only the column the migrations' foreign keys reference.
create table if not exists auth.users (
    id uuid primary key
);

-- Supabase's definition: the JWT subject, read from either setting form.
-- app/db/session.py binds request.jwt.claims once per transaction.
create or replace function auth.uid() returns uuid
language sql stable
as $$
  select coalesce(
    nullif(current_setting('request.jwt.claim.sub', true), ''),
    (nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'sub')
  )::uuid
$$;

revoke all on function auth.uid() from public;

-- The test owners. test_paper_lifecycle_postgres.py uses exactly these
-- ids, plus a third that is deliberately absent (the foreign-key case).
insert into auth.users (id) values
    ('a1a1a1a1-6666-4666-8666-a1a1a1a1a1a1'),
    ('b2b2b2b2-6666-4666-8666-b2b2b2b2b2b2')
on conflict do nothing;
