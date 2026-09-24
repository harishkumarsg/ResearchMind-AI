-- ---------------------------------------------------------------------
-- 0006: paper_relationships — one generated cross-paper relationship
-- analysis per unordered pair of a user's own papers (Phase 2C Step 2C-3).
--
-- NOT YET APPLIED. Run this as the `postgres` role (Supabase SQL editor
-- or a postgres connection) BEFORE deploying code that reads it.
-- NOT executed by the application.
--
-- Additive and non-destructive: one new table, one check constraint, one
-- unique constraint, one policy, one grant. No existing table, column,
-- index, policy or row is altered or dropped. Touches Postgres only —
-- Qdrant, Supabase Storage and the indexed vector data are entirely
-- unaffected.
--
-- Preconditions inherited from earlier migrations, not re-declared here:
--   * pgcrypto is installed by 0001, so gen_random_uuid() is available.
--   * papers and auth.users already exist (0001).
-- ---------------------------------------------------------------------

-- ---------------------------------------------------------------------
-- WHY A NEW TABLE
--
-- paper_intelligence is keyed UNIQUE(owner_id, paper_id) and its jsonb is
-- contractually a ten-section PaperIntelligence. A relationship has a
-- different key (a PAIR), a different shape, and different invalidation,
-- so storing one there would break the guarantee that every row in that
-- table is a valid PaperIntelligence. `reports` is a single-owner
-- markdown artefact and is equally unsuitable.
--
-- WHAT THE JSONB HOLDS
--
-- Exactly one serialized PaperRelationship, already validated by
-- app/services/relationship_schema.py: a map of canonical section name ->
-- {relation, statement, cites_a, cites_b}. The database does not
-- re-implement that validation — the Pydantic model is the single source
-- of truth, and a jsonb CHECK restating it would drift from the model the
-- first time the schema version moves.
--
-- The jsonb deliberately carries NO identity: no owner_id, no paper ids,
-- no Qdrant point ids, no tokens. Those are columns, or they do not exist.
-- `extra="forbid"` at every level of the Pydantic model is what makes
-- that a guarantee rather than a hope.
-- ---------------------------------------------------------------------
create table if not exists paper_relationships (
    id              uuid primary key default gen_random_uuid(),
    owner_id        uuid        not null references auth.users(id) on delete cascade,

    -- ON DELETE CASCADE on both: delete_paper.py issues an ORM delete of
    -- the papers row, and every relationship that mentioned that paper
    -- goes with it. No application code needs to remember to clean up,
    -- which is why this is a cascade rather than a restrict.
    paper_a_id      uuid        not null references papers(id)     on delete cascade,
    paper_b_id      uuid        not null references papers(id)     on delete cascade,

    relationship    jsonb       not null,

    -- -----------------------------------------------------------------
    -- SOURCE PROVENANCE. The `generated_at` of the two paper_intelligence
    -- rows this analysis was derived from.
    --
    -- Without these, a persisted relationship is undetectably stale: if
    -- either paper is re-analysed, the claims this row compares no longer
    -- exist, and nothing in the row would say so. `schema_version` does
    -- not cover it (the shape is unchanged) and neither does
    -- `generated_at` below (that moves only when the RELATIONSHIP is
    -- regenerated, not when its inputs are).
    --
    -- NOT NULL because a relationship with unknown provenance cannot be
    -- checked for staleness, which defeats the purpose of storing it.
    --
    -- ORIENTATION: paper_a_generated_at belongs to paper_a_id and
    -- paper_b_generated_at to paper_b_id — the CANONICAL ids, matching
    -- the relationship jsonb's own cites_a/cites_b orientation. The store
    -- refuses a non-canonical pair rather than reorienting anything, so
    -- the association cannot drift.
    -- -----------------------------------------------------------------
    paper_a_generated_at timestamptz not null,
    paper_b_generated_at timestamptz not null,

    -- When the RELATIONSHIP generation itself began, as opposed to when
    -- the row was written or when its sources were produced. Distinct from
    -- created_at/updated_at and from the two source stamps above, and
    -- stamped by the application at RUN START, which is what makes the
    -- store's stale-write guard order concurrent regenerations correctly.
    generated_at    timestamptz not null,

    model           text        not null,
    schema_version  text        not null,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now(),

    -- -----------------------------------------------------------------
    -- CANONICAL PAIR. This one constraint does two jobs.
    --
    -- 1. It makes (A,B) and (B,A) impossible to store as two rows. The
    --    relationship between two papers is unordered as a product fact —
    --    the /compare page lets a reader swap A and B freely — so two
    --    rows for one pair would be two competing analyses of the same
    --    thing, with nothing to say which is current.
    --
    -- 2. A strict `<` also forbids paper_a_id = paper_b_id, so a paper
    --    can never be related to itself. A separate `<>` check would be
    --    redundant.
    --
    -- Enforced in the DATABASE rather than only in application code
    -- because it is an invariant about stored rows, not about one code
    -- path: a future writer that forgot to canonicalize would be
    -- rejected here instead of quietly creating the duplicate.
    --
    -- uuid comparison in Postgres is byte-wise over the 16 bytes, which
    -- is the same order Python's uuid.UUID comparison produces, so the
    -- application and the database agree on which id is "smaller".
    -- -----------------------------------------------------------------
    constraint paper_relationships_canonical_pair check (paper_a_id < paper_b_id),

    -- Exactly one CURRENT analysis per owner per unordered pair. This is
    -- what makes the store an upsert rather than an append log, and what
    -- makes a concurrent double-generation resolve to one row.
    -- owner_id leads the tuple deliberately — see the index note below.
    unique (owner_id, paper_a_id, paper_b_id)
);

-- ---------------------------------------------------------------------
-- No separate index on owner_id, on purpose.
--
-- The UNIQUE constraint above is backed by a btree on
-- (owner_id, paper_a_id, paper_b_id), and every read this table serves is
-- either the full triple or an owner-scoped scan — both served by that
-- index as a leftmost-prefix match. A standalone owner_id index would be
-- redundant and would cost a write on every upsert.
--
-- The same reasoning as 0005 (which omits it because owner_id leads its
-- UNIQUE) and 0004 (whose composite primary key covers it). chat_messages
-- DOES carry a separate owner_id index, because there owner_id is not the
-- leftmost column of any composite — here it is.
--
-- No index on paper_a_id / paper_b_id either. Nothing queries this table
-- by one paper alone today; the FK cascades do not need one, and a
-- speculative index is a write cost with no reader.
-- ---------------------------------------------------------------------

-- ---------------------------------------------------------------------
-- PRIVILEGE MODEL — same footing as the six existing tables.
--
-- Established by 0004 and unchanged since: researchmind_app has no role
-- memberships, is NOINHERIT and is NOT covered by the default privileges
-- for schema public, so a new table arrives with NO grant for it. This
-- migration must therefore grant explicitly.
--
-- SELECT, INSERT and UPDATE — the three the store actually uses:
--   select  — get_relationship
--   insert  — save_relationship, first generation
--   update  — save_relationship, regeneration
--
-- DELIBERATELY NO DELETE, unlike 0005. There is no delete_relationship:
-- a regeneration replaces the row in place, and removal happens only by
-- cascade when a papers or auth.users row goes. A cascade runs with the
-- referencing table's privileges and needs no grant for the app role at
-- all, so granting DELETE here would widen the role for a code path that
-- does not exist. This mirrors usage_counters, which is granted no DELETE
-- for the same reason.
--
-- Also deliberately absent: TRUNCATE, REFERENCES, TRIGGER, MAINTAIN, any
-- grant to public / anon / authenticated / service_role, any role
-- creation or alteration, and any SECURITY DEFINER function.
--
-- researchmind_app already holds USAGE on schema public.
-- ---------------------------------------------------------------------

-- ---------------------------------------------------------------------
-- Row Level Security — enabled AND forced, identical in shape to 0005.
--
-- FORCE is what makes this real rather than decorative: without it a
-- table owner is exempt from its own policies. Combined with
-- researchmind_app being NOBYPASSRLS and the per-transaction
-- request.jwt.claims binding in app/db/session.py, auth.uid() resolves to
-- the verified JWT sub for the duration of one transaction and to nothing
-- at all outside it.
--
-- Both USING and WITH CHECK, and for the same reason as 0005: this table
-- is written by an upsert, which is an INSERT and an UPDATE. USING alone
-- would govern what an owner may read and which rows it may update while
-- leaving it free to INSERT a row attributed to another owner_id.
--
-- A transaction with no claim bound sees auth.uid() as null, so
-- `owner_id = auth.uid()` is null for every row and the table is empty
-- and unwritable. That is the intended failure mode: a connection that
-- never went through _new_session() gets nothing, rather than everything.
--
-- The policy deliberately does NOT reach into papers to re-check that
-- paper_a_id and paper_b_id belong to auth.uid(). That check belongs in
-- the application (_owns_both_papers in paper_relationship_store.py,
-- mirroring paper_intelligence_store and chat_store), exactly as it does
-- for paper_intelligence.paper_id and chat_sessions.current_paper_id. A
-- cross-table subquery in a policy would run per row on every read to
-- defend against rows an owner could only ever plant in their own
-- partition.
--
-- This migration adds policies to a new table and alters no existing one.
-- ---------------------------------------------------------------------
alter table paper_relationships enable row level security;
alter table paper_relationships force row level security;

drop policy if exists paper_relationships_owner_only on paper_relationships;
create policy paper_relationships_owner_only on paper_relationships
    using (owner_id = auth.uid())
    with check (owner_id = auth.uid());

grant select, insert, update on paper_relationships to researchmind_app;
