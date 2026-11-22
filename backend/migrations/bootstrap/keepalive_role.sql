-- bootstrap/keepalive_role.sql  (apply once by hand, AFTER 0007)
--
-- Creates the role used only by .github/workflows/keepalive.yml, which runs
--   select count(*) from public.papers
-- every two days so the free-tier Supabase project sees real database
-- activity and is not paused.
--
-- Least privilege:
--   * can log in, nothing else (no RLS bypass, no inheritance, no DDL);
--   * SELECT on public.papers only;
--   * every session is read-only and time-limited;
--   * at most two concurrent connections.
--
-- RLS stays forced on papers. The papers_owner_only policy applies to every
-- role and compares owner_id with auth.uid(). This role never sends a JWT,
-- so auth.uid() is NULL and the count is always 0: the query runs, but no
-- row is visible. The policy calls auth.uid(), so the role needs EXECUTE on
-- it, exactly as 0007 grants it to researchmind_app; without it the query
-- can fail with "permission denied for function uid" instead of returning 0.
-- No USAGE on schema auth is granted: a stored policy already refers to the
-- function directly, and on Supabase that grant has no effect anyway
-- (docs/MASTER_ROADMAP.md, section 7, item 9).
--
-- Idempotent: safe to run again.
--
-- The password is deliberately NOT set here. Generate one, then run:
--   alter role researchmind_keepalive with password '<generated secret>';
-- and store the resulting connection string only as the GitHub secret
-- KEEPALIVE_DATABASE_URL.

do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'researchmind_keepalive') then
    create role researchmind_keepalive
      login
      nobypassrls
      noinherit
      nosuperuser
      nocreaterole
      nocreatedb
      noreplication
      connection limit 2;
  end if;
end
$$;

alter role researchmind_keepalive set default_transaction_read_only = on;
alter role researchmind_keepalive set statement_timeout = '10s';
alter role researchmind_keepalive set idle_in_transaction_session_timeout = '10s';

grant usage on schema public to researchmind_keepalive;
grant select on table public.papers to researchmind_keepalive;

grant execute on function auth.uid() to researchmind_keepalive;

-- Check (run as the new role, e.g. from psql with its connection string):
--   select count(*) from public.papers;   -- expect 0, no error
--   select count(*) from public.reports;  -- expect: permission denied
