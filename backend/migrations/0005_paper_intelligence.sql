-- ---------------------------------------------------------------------
-- 0005: paper_intelligence — one current structured Paper Intelligence
-- object per owned paper (Phase 2B Step 2).
--
-- NOT YET APPLIED. Run this as the `postgres` role (Supabase SQL editor
-- or a postgres connection) BEFORE deploying the code that reads it.
-- NOT executed by the application.
--
-- Additive and non-destructive: one new table, one unique constraint,
-- one policy, one grant. No existing table, column, index, policy or row
-- is altered or dropped. Touches Postgres only — Qdrant, Supabase
-- Storage and the indexed vector data are entirely unaffected.
--
-- Preconditions inherited from earlier migrations, not re-declared here:
--   * pgcrypto is installed by 0001, so gen_random_uuid() is available.
--   * papers and auth.users already exist (0001).
-- ---------------------------------------------------------------------

-- ---------------------------------------------------------------------
-- WHAT THIS TABLE STORES, AND WHAT IT DELIBERATELY DOES NOT
--
-- `intelligence` holds exactly one serialized PaperIntelligence object,
-- already validated by app/services/intelligence_schema.py. The database
-- does not re-implement that validation: the Pydantic model is the
-- single source of truth for the JSON structure, and a jsonb CHECK
-- constraint attempting to restate it would drift from the model the
-- first time the schema version moves.
--
-- There is no evidence_pages column. Page numbers already live inside
-- the evidence entries; a denormalised copy would be a second source of
-- truth for the same fact, and the only interesting failure mode of such
-- a column is the two disagreeing.
--
-- No Qdrant point id is stored anywhere in this table. Point ids are
-- regenerated on every index run (index_document.py assigns a fresh
-- uuid4 per chunk), so one persisted here would silently rot into a
-- dangling reference after the next re-index. Durable evidence identity
-- is the (page, chunk_id) pair, which is stable for a given extraction
-- of a given paper, and it lives inside the jsonb.
-- ---------------------------------------------------------------------
create table if not exists paper_intelligence (
    id              uuid primary key default gen_random_uuid(),
    owner_id        uuid        not null references auth.users(id) on delete cascade,
    -- ON DELETE CASCADE is the whole invalidation story for a removed
    -- paper: delete_paper.py issues an ORM delete of the papers row, and
    -- the intelligence for that paper goes with it. No application code
    -- needs to remember to clean up, which is why this is a cascade and
    -- not a SET NULL or a restrict.
    paper_id        uuid        not null references papers(id)     on delete cascade,
    intelligence    jsonb       not null,
    -- When the model produced this object. Distinct from created_at /
    -- updated_at, which record when the ROW was written: a regeneration
    -- moves both, but only generated_at is a statement about the
    -- content, and it is what a future invalidation rule ("regenerate
    -- anything older than X") would compare against.
    generated_at    timestamptz not null,
    -- The model identifier and the schema version the object was
    -- produced under. Both are recorded so a bad generation can be
    -- traced to its producer, and so a future schema_version bump can
    -- select exactly the rows that need regenerating.
    model           text        not null,
    schema_version  text        not null,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now(),
    -- Exactly one CURRENT row per owned paper. This is what makes the
    -- store an upsert rather than an append log, and it is what makes a
    -- concurrent double-generation resolve to one row instead of two.
    -- owner_id leads the pair deliberately — see the index note below.
    unique (owner_id, paper_id)
);

-- ---------------------------------------------------------------------
-- No separate index on owner_id, on purpose.
--
-- The UNIQUE constraint above is backed by a btree on
-- (owner_id, paper_id), and every read this table serves is either the
-- full pair or an owner-scoped scan — both served by that index as a
-- leftmost-prefix match. A standalone owner_id index would be redundant
-- and would cost a write on every upsert.
--
-- This follows the reasoning already applied twice in this schema:
-- chat_messages has no standalone session_id index because
-- (session_id, created_at) covers it, and usage_counters has no index
-- beyond its composite primary key. chat_messages DOES carry a separate
-- owner_id index, because there owner_id is not the leftmost column of
-- any composite — here it is.
-- ---------------------------------------------------------------------

-- ---------------------------------------------------------------------
-- PRIVILEGE MODEL — same footing as the five existing tables.
--
-- Established by 0004 and unchanged since: researchmind_app has no role
-- memberships, is NOINHERIT and is NOT covered by the default privileges
-- for schema public, so a new table arrives with NO grant for it. This
-- migration must therefore grant explicitly.
--
-- SELECT, INSERT, UPDATE and DELETE — the four the store actually uses:
--   select  — get_intelligence
--   insert  — save_intelligence, first generation
--   update  — save_intelligence, regeneration
--   delete  — delete_intelligence
-- This matches papers / reports / chat_sessions / chat_messages, which
-- carry researchmind_app=arwd. It is broader than usage_counters, which
-- is granted no DELETE because nothing ever deletes a counter; here a
-- delete path exists and is exercised.
--
-- Note the cascade above is NOT the reason for the DELETE grant: a
-- cascade from papers or auth.users runs with the referencing table's
-- privileges and needs no grant for the app role at all.
--
-- researchmind_app already holds USAGE on schema public.
-- ---------------------------------------------------------------------

-- ---------------------------------------------------------------------
-- Row Level Security — enabled AND forced, matching usage_counters and
-- the post-C1 posture of the four core tables.
--
-- FORCE is what makes this real rather than decorative: without it a
-- table owner is exempt from its own policies. Combined with
-- researchmind_app being NOBYPASSRLS and the per-transaction
-- request.jwt.claims binding in app/db/session.py, auth.uid() resolves
-- to the verified JWT sub for the duration of one transaction and to
-- nothing at all outside it.
--
-- Both USING and WITH CHECK, and for the same reason as usage_counters:
-- this table is written by an upsert, which is an INSERT and an UPDATE.
-- USING alone would govern what an owner may read and which rows it may
-- update, while leaving it free to INSERT a row attributed to another
-- owner_id.
--
-- A transaction with no claim bound sees auth.uid() as null, so
-- `owner_id = auth.uid()` is null for every row and the table is empty
-- and unwritable. That is the intended failure mode: a connection that
-- never went through _new_session() gets nothing, rather than
-- everything.
--
-- The policy deliberately does NOT reach into papers to re-check that
-- paper_id belongs to auth.uid(). That check belongs in the application
-- (_owns_paper in paper_intelligence_store.py, mirroring chat_store),
-- exactly as it does for chat_sessions.current_paper_id — the existing
-- convention for a paper reference. A cross-table subquery in a policy
-- would run per row on every read of this table to defend against a row
-- an owner could only ever plant in their own partition.
--
-- This migration adds policies to a new table and alters no existing one.
-- ---------------------------------------------------------------------
alter table paper_intelligence enable row level security;
alter table paper_intelligence force row level security;

drop policy if exists paper_intelligence_owner_only on paper_intelligence;
create policy paper_intelligence_owner_only on paper_intelligence
    using (owner_id = auth.uid())
    with check (owner_id = auth.uid());

grant select, insert, update, delete on paper_intelligence to researchmind_app;
