-- 0007_core_tables_force_rls.sql
--
-- Brings the four core tables to the same state the later tables reach in
-- their own migrations (0004-0006): privileges granted to the app role and
-- row-level security FORCED, so policies apply even to the table owner.
--
-- On production these statements were applied by hand during C1 (see
-- docs/MASTER_ROADMAP.md). Recording them here means a fresh environment
-- gets the same tenant-isolation guarantees as production. Every statement
-- is idempotent, so re-running it on production changes nothing.

grant usage on schema public to researchmind_app;

grant select, insert, update, delete
  on papers, reports, chat_sessions, chat_messages
  to researchmind_app;

-- Policies call auth.uid(); the app role must be able to execute it.
grant execute on function auth.uid() to researchmind_app;

alter table papers        force row level security;
alter table reports       force row level security;
alter table chat_sessions force row level security;
alter table chat_messages force row level security;
