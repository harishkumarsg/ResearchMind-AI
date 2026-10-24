-- bootstrap/app_role.sql  (apply once, BEFORE 0001)
--
-- Creates the least-privilege role the backend connects as.
--
-- Why this file exists: migrations 0004-0006 grant privileges to
-- researchmind_app, but the role itself was created by hand on the
-- production project (see docs/MASTER_ROADMAP.md, C1). On a fresh
-- Supabase project, 0004 fails with "role does not exist".
-- Apply this file first, then 0001 -> 0007 apply cleanly from zero. It lives
-- outside the numbered sequence so the existing migration-order tests hold.
--
-- Idempotent: on the existing project, where the role already exists,
-- it does nothing.
--
-- The password is deliberately NOT set here. Set it out of band, from
-- your secret manager, after applying this file:
--   alter role researchmind_app with password '<generated secret>';

do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'researchmind_app') then
    create role researchmind_app
      login
      nobypassrls
      noinherit
      nosuperuser
      nocreaterole
      nocreatedb
      noreplication;
  end if;
end
$$;
