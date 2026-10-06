# ResearchMind AI — Master Project Roadmap

**Last updated:** 2026-09-27
**Status of this document:** current and authoritative.

> **Reconciled 2026-09-27.** This document was finalized at `25064eb` (2026-09-14)
> and then went stale: **44 commits** landed after it, including three migrations
> and roughly 28 new backend test files, and the product was deployed. A
> read-only reconciliation audit against git history restored the record. Section
> 2 now carries the later work, §3 replaces the former "NOT DEPLOYED" state, and
> §§7–8 correct backlog items that were resolved or whose line numbers drifted.
>
> **No future phase was added.** None is defined anywhere in the repository, and
> inventing one would be fabrication. That remains a valid state.

> **Older documents removed.** `docs/ROADMAP.md` (a pre-security sprint list),
> `docs/ARCHITECTURE.md` (empty), `docs/API_REFERENCE.md` (listed endpoints that
> no longer exist in that form) and `docs/PROJECT_CONTEXT.md` (described the
> replaced Sentence Transformers / Cross Encoder / Ollama stack, unauthenticated
> local storage, and the rule "Do not replace Ollama") were deleted on 2026-09-14.
> They remain in git history at `25064eb`. For the live API surface, use the
> backend's generated OpenAPI docs at `/docs`.
>
> Every claim below is tied to a durable artifact — a test file, a migration,
> a code location, a commit, or a recorded verification run. Where something
> could not be verified, it says so rather than guessing.

---

## 1. Project overview

### Purpose

ResearchMind AI is a multi-tenant academic research workspace. A signed-in
researcher uploads PDFs into a **private** library, has them indexed into a
vector database, then performs semantic search, asks grounded questions with
citations, compares papers, and generates and exports research reports.

The defining constraint of the current codebase is **tenant isolation**: a
user's papers, vectors, retrieval results, citations, reports and conversation
must be reachable only by that user, enforced by the backend, never by the
client.

### High-level architecture

```
Browser (TanStack Start / React 19)
   │  Supabase Auth — Google OAuth; JWT in Authorization header
   ▼
FastAPI backend  ← the application authorization boundary
   ├── app/core/auth.py      JWT verified via JWKS; owner_id = verified `sub`
   ├── app/api/*             endpoints; every query filtered by owner_id
   ├── app/services/         storage.py (Supabase Storage), chat_store.py
   ├── app/rag/              embedder (Voyage), vector_store (Qdrant), reranker
   └── app/db/               SQLAlchemy models + session factory + JWT claims
   │
   ├── Supabase Postgres     papers, reports, chat_sessions, chat_messages,
   │                         plus usage counters (0004), paper intelligence (0005)
   │                         and paper relationships (0006)
   │                         app role researchmind_app (NOBYPASSRLS), RLS FORCED
   ├── Supabase Storage      bucket "papers", path {owner}/{paper}/original.pdf
   ├── Qdrant Cloud          collection researchmind_v2
   ├── Voyage AI             embeddings + reranking
   └── Groq                  answer generation
```

**`owner_id` is derived in exactly one place** — `app/core/auth.py`, from the
verified JWT `sub`. No endpoint accepts an owner identifier from the client.

Tenant isolation is enforced at **two independent layers**: an explicit
`WHERE owner_id = :owner_id` in every query, and Postgres row-level security
driven by the same verified identity.

### Established technology — local validated build

This describes the validated product. **Corrected 2026-09-27:** this section
previously said "the deployed code differs substantially", which was true when
`origin/master` was `d83a16f` and is no longer — `origin/master` now equals `HEAD`
(`ddd6818`). See §3 for the deployment state and its SHA-verification limitation.

| Layer | Choice | Notes |
|---|---|---|
| Backend | FastAPI 0.136.3 / Starlette 1.2.1, Python 3.11 | live config is `backend/railway.toml` → `uvicorn app.main:app`; the root `render.yaml` is stale (§7 item 13) |
| ORM / driver | SQLAlchemy 2.0.50, psycopg 3.3.5 | `DATABASE_URL` uses `postgresql+psycopg://` |
| Database | Supabase Postgres **17.6** | session-mode pooler, port 5432 |
| Database role | `researchmind_app` | NOBYPASSRLS, owns no tables; username `researchmind_app.<project_ref>` |
| Row-level security | enabled **and forced** on all **seven** tables | owner-only policies, `USING (owner_id = auth.uid())`. C1 forced the original four; `usage_counters` (0004), `paper_intelligence` (0005) and `paper_relationships` (0006) each ship `enable` + `force row level security` and an owner-only policy in their own migration |
| Auth | Supabase Auth, Google OAuth only | JWKS verification, RS256/ES256 allowlist |
| Object storage | Supabase Storage, bucket `papers` | RLS policies in migration 0002 |
| Vectors | Qdrant Cloud, `researchmind_v2` | 1024-dim, COSINE, 8 payload indexes |
| Embeddings | Voyage `voyage-4` | token-budget batching + rate-limit backoff |
| Reranking | Voyage `rerank-3` | **disabled by default** (`RERANK_ENABLED=false`) |
| Generation | Groq `openai/gpt-oss-120b` | streamed via SSE |
| Frontend | TanStack Start, React 19, Vite 7.3.5 | React Query; Vitest 5 |

Retrieval constants (`ask_stream.py`): `SEARCH_LIMIT=8`, `TOP_CHUNKS=4`,
`MAX_CONTEXT=4000`. History window (`chat_store.py`): `HISTORY_MESSAGE_LIMIT=6`.

---

## 2. Completed phases

### Phase 0 — Baseline

- **Objective:** record the starting state before any change, without altering it.
- **Implemented:** read-only infrastructure and offline baseline checks, including
  the then-known defect that the hardcoded Groq model was unavailable to the account.
- **Evidence:** `tests/test_phase0_baseline_infra.py`,
  `tests/test_phase0_baseline_offline.py`. Live-infra checks are gated behind
  `RUN_LIVE_INFRA_TESTS=1` (`tests/live_infra.py`) so they skip by declaration
  rather than by a swallowed connection error.
- **Status:** COMPLETE.

### Phase 1 — Safety fixes

- **Objective:** remove destructive and unsafe behaviour from the ingest path.
- **Implemented:** `delete_collection()` removed from indexing; `/index-document`
  made POST-only with the duplicate route deleted; upload validation (extension,
  PDF magic bytes, size limit); Groq model corrected in both call sites; the
  frontend switched to POST.
- **Evidence:** `tests/test_phase1_safety_fixes.py`, including a runtime assertion
  that `delete_collection` is never invoked, plus a source-level check that the
  call does not appear in `index_document.py`.
- **Status:** COMPLETE.

### Phase 1.5 — Authentication and ownership

- **Objective:** introduce real identity and make the backend the sole
  authorization boundary.
- **Implemented:** Supabase JWT verification via JWKS with an algorithm allowlist,
  pinned issuer, `audience="authenticated"` and required `exp`/`sub`/`iat`;
  `owner_id` derived only from the verified `sub`; core schema created
  (`papers`, `reports`, `chat_sessions`); owner-scoped Storage paths.
- **Evidence:** `tests/test_phase1_5_auth.py`, `tests/test_phase1_5_ownership.py`,
  `migrations/0001_core_schema.sql` (applied).
- **Status:** COMPLETE.

### Phase 2 — Multi-tenant retrieval and evidence integrity

- **Objective:** make every retrieval path owner-scoped, and make displayed
  sources honestly reflect what the model was given.
- **Implemented:** server-constructed `owner_id` filter on every Qdrant operation;
  collection `researchmind_v2` with payload indexes; user-JWT-scoped Storage
  client so Storage RLS is a real second layer; rate-limit-aware Voyage embedding
  (token-budget batching, pacing, bounded backoff on `RateLimitError` only);
  idempotent retry that reuses an existing Storage object rather than
  overwriting; per-owner in-process memory replacing shared globals; and the
  evidence-integrity fixes (point-id dedupe, budget skip-and-continue,
  1:1 citations, page-labelled context, `cited` flag, citation suppression on
  refusal).
- **Evidence:** nine `tests/test_phase2_*.py` files,
  `tests/test_storage_user_scoped_auth.py`, `migrations/0002_storage_policies.sql`
  (applied). The evidence invariant — *the exact passages given to the model are
  the exact passages shown as Sources* — is enforced in code by a single
  `selected` list in `ask_stream.py`, not by prompt instruction.
- **Status:** COMPLETE.

### D1 — Frontend cache isolation

- **Objective:** stop one user's cached data being visible to the next user in
  the same browser session.
- **Implemented:** user-scoped React Query keys (`src/lib/query-keys.ts`) and a
  cache clear driven by a change of **user id** rather than by auth event type
  (`src/lib/auth-context.tsx`), including the search-key prefix-collision fix.
- **Evidence:** `src/lib/query-keys.test.ts`, `src/lib/auth-context.test.tsx`;
  browser QA passed.
- **Status:** COMPLETE.

### D2 — Durable persistence

- **Objective:** move reports and conversation state out of in-process memory and
  ephemeral disk into Postgres, so they survive restarts and are owner-scoped in
  storage as well as in code.

- **Implemented, in seven approved steps:**
  1. Public session factory — `get_session_factory()` and a transactional
     `session_scope()` in `app/db/session.py`.
  2. Report persistence — `/research` writes a `reports` row (owner from JWT,
     query, markdown, citations as JSONB).
  3. Export from Postgres — `/export-report` reads the owner's most recent
     report; the `latest_report_*.txt` / `latest_sources_*.txt` fallback was
     deleted, and the PDF is now built in memory (`io.BytesIO`) and returned as
     bytes, leaving nothing on disk.
  4. Chat persistence writes — `chat_store.py` with `persist_user_turn` and
     `persist_assistant_turn`, each in its own short-lived transaction so no
     pooled connection is held across the LLM stream.
  5. Chat reads from Postgres — `load_chat_state()` reconstructs history, topic
     and current paper; the follow-up paper lock keys on `paper_id`, not title.
  6. Full-stack verification — backend suite, frontend tests, production build.
  7. Cleanup — `/summarize-paper` persists `current_paper_id` via
     `set_current_paper()`; the in-memory title fallback was removed; seven dead
     `UserMemory` chat fields deleted.

- **Schema:** `migrations/0003_chat_messages.sql` — `chat_messages` table,
  `UNIQUE (owner_id)` on `chat_sessions`, two indexes, RLS enabled with an
  owner-only policy. Applied 2026-09-13, additive and non-destructive.

- **Validation evidence:**
  - Offline suite: **254 tests, OK (skipped=7)**, deterministic across runs.
  - A tripwire run replacing `app.db.session.create_engine` proved **no test ever
    builds a real engine** — 254 tests, 0 failures, tripwire never fired.
  - Frontend: 41 tests passing (3 files); production build succeeds.
  - Live Postgres read-only audit (8A) confirmed schema, constraints, indexes,
    FK `ON DELETE` behaviour, RLS state, and dialect behaviour (`timestamptz`
    returns tz-aware, `jsonb` round-trips to a Python list, `uuid` native).
  - Live persistence smoke test (8B Stage 1) exercised the real production
    functions and proved, on real Postgres, the constraints SQLite never
    enforced: `UniqueViolation` on a duplicate session, `ForeignKeyViolation` on
    a fabricated owner, and successful retry recovery. All rows removed
    afterwards; counts restored exactly.
  - One controlled real end-to-end request (8B Stage 2) through Qdrant, Voyage,
    Groq and Postgres; rows verified then deleted.
  - Real Google-authenticated browser QA passed, including the decisive test:
    after a page refresh a follow-up question was still correctly grounded, and
    `current_topic` was **not** overwritten — proving follow-up detection read
    from Postgres, since no in-process chat state exists any more.

- **Committed:** local commit `e144ab0` (D0–D2 checkpoint).
- **Status:** COMPLETE / PASS.

### C1 — RLS Hardening

- **Objective:** make Postgres row-level security a real second enforcement layer
  beneath the application's `WHERE owner_id` filters.

- **Implemented, in five approved stages:**
  1. **Read-only audit** — RLS proven inert for three independent reasons: the
     app connected as `postgres`, which owned all four tables, carried
     `BYPASSRLS`, and `FORCE` was off.
  2. **Role** — `researchmind_app`: LOGIN, NOBYPASSRLS, NOINHERIT, NOSUPERUSER,
     NOCREATEROLE, NOCREATEDB, NOREPLICATION; owns no tables; granted `USAGE` on
     `public`, `SELECT/INSERT/UPDATE/DELETE` on the four tables, and `EXECUTE` on
     `auth.uid()`. No `TRUNCATE`, no DDL, no access to `auth.users`. Supavisor
     accepts it as `researchmind_app.<project_ref>` on the session pooler.
  3. **Claims plumbing** — a SQLAlchemy `after_begin` session event sets
     `request.jwt.claims` with `set_config(..., true)` (transaction-local) on
     **every** transaction start, so identity survives the multiple commits in
     `/upload`, `/index-document` and `/paper/{name}`.
     `session_scope(owner_id)` and
     `get_db_session(owner_id=Depends(get_current_owner_id))` require the
     verified owner. Local commit `cebd105`.
  4. **`DATABASE_URL` switched** to `researchmind_app` — local `backend/.env`
     only; one line changed, all other secrets untouched.
  5. **`FORCE ROW LEVEL SECURITY`** on `papers`, `reports`, `chat_sessions` and
     `chat_messages`. Policies were not altered; explicit `WITH CHECK` (5b) was
     deliberately excluded.

- **Validation evidence:**
  - Offline suite: **270 tests, OK (skipped=7)**; real-engine tripwire never fired.
    Includes 16 claims tests and a source guard forbidding session-level `SET`.
  - **Pooled-connection leakage:** with `pool_size=1`, every scenario shared one
    backend pid. Owner A, then owner B, then a no-claims session: B saw only B,
    the no-claims session saw zero rows, and identity re-bound correctly after
    each commit.
  - **Pre-FORCE probe:** `postgres` still saw every row while claiming owner B,
    confirming it bypasses via the `BYPASSRLS` attribute, so FORCE does not affect
    migrations or admin access.
  - **Write matrix W1–W12, run before and after FORCE, identical both times.**
    Own insert and update succeed. Cross-owner insert, owner reassignment,
    no-claims insert and a foreign-owner chat message are rejected (`42501`).
    Cross-owner read, update and delete affect zero rows. The `chat_messages`
    FK to `auth.users` is accepted without `SELECT` on `auth.users`. Every write
    ran inside a rolled-back transaction.
  - **Part 1a — real endpoints as the app role:** `/ask-stream` and `/research`
    wrote chat and report rows correctly; owner B saw none of owner A's freshly
    created data. Artifacts deleted by exact id; the mutated session row restored
    field-for-field.
  - **Part 2 — real Google-authenticated browser:** a throwaway fixture was
    uploaded through Storage RLS with the user's own JWT and auto-indexed
    (1 point, owner A). Its deterministic `paper_id` matched the value predicted
    before upload. A question, a follow-up, and a follow-up after a page refresh
    were all grounded on the fixture. Deletion through the UI removed the
    Postgres row, the Qdrant point and the Storage object. Chat artifacts were
    deleted by exact id and the session row restored field-for-field.
    (`/upload` and `/index-document` could not run under a test dependency
    override because Storage RLS requires a genuine user JWT, so they were
    validated here instead.)
  - **Closing verification:** baseline restored exactly; RLS enabled and forced on
    all four tables; the four policies byte-identical to their originals; no
    migration changed.

- **Not verified:**
  - **Owner B browser sign-in was not performed** — no second-account session was
    run. Cross-owner isolation is verified at the database and endpoint layers only.
  - True concurrency on the `UNIQUE(owner_id)` race — the retry was exercised with
    a real `UniqueViolation`, but the two attempts were sequential, not raced.

- **Committed:** commit `cebd105`. *(At the time of writing this was local only. It
  is now an ancestor of `HEAD` and pushed — verified with
  `git merge-base --is-ancestor`.)*
- **Status:** COMPLETE / PASS.

---

## 2b. Completed work after C1

C1 was the last item defined as a *major phase*. The work below landed afterwards
in smaller, separately approved steps. Some carries its own label and some does
not; the labels used here are the ones that actually appear in the repository, and
none is invented. Each row is tied to a commit and to test or migration evidence.

### Production hardening and capability series

| Work | Commit | Evidence |
|---|---|---|
| CORS allowlist — replaced a non-matching wildcard origin | `7dd86f0` | `app/core/cors.py`, `tests/test_cors_config.py` |
| Quotas and rate limits | `265c471` | `migrations/0004_usage_counters.sql`, five `tests/test_e1_*.py` |
| E2 — Ask questions spanning multiple papers | `9d63bb1` | `tests/test_e2_multi_paper_retrieval.py` |
| E2b — multi-paper evidence diversity within the context budget | `3efbda1` | `tests/test_e2b_evidence_diversity.py` |
| P1 — provider timeouts and error handling | `5ce8624` | `tests/test_p1_provider_timeouts.py` |
| P2A — prevent silent incomplete generations | `df5220a` | `tests/test_p2a_generation_budgets.py` |
| P2B — per-caller generation budgets for Report, Compare, Summarize | `8b25f37` | `tests/test_p2b_caller_budgets.py` |
| Error sanitization — unclassified internal errors | `8a49ea9` | `tests/test_internal_error_sanitization.py` |

### Phase 1A — Quota and paper UX hardening

- **Implemented:** quota and paper-UX hardening following the P-series.
- **Evidence:** commit `c42ddf5`, `tests/test_phase1a_hardening.py`.
- **Status:** COMPLETE.

### Phase 2A — Paper Research Workspace

- **Objective:** a per-paper workspace rather than a single global Ask surface.
- **Evidence:** commits `561caf4`, `39da455` (signed-out redirect for protected
  routes), `tests/test_phase2a_paper_workspace.py`.
- **Status:** COMPLETE.

### Phase 2B — Structured Paper Intelligence

- **Objective:** a validated ten-section structured analysis of one paper, where
  nothing is persisted that did not survive validation against an allowlist built
  from the exact chunks supplied to the model.
- **Implemented, in five approved steps:** schema and validator; persistence;
  generation pipeline; persisted reader; Evidence Inspector.
- **Schema:** `migrations/0005_paper_intelligence.sql` (applied).
- **Evidence:** commits `65492e4`, `31388eb`, `5eb1342`, `9c1ba11`, `12ff9d3`;
  `tests/test_phase2b_intelligence_schema.py`,
  `tests/test_phase2b_intelligence_persistence.py`,
  `tests/test_phase2b_intelligence_generation.py`,
  `tests/test_phase2b_intelligence_read.py`.
- **Invariant, enforced in code:** the validator trusts no `owner_id`, `paper_id`,
  paper title or Qdrant point id from the model; evidence identity is
  `(page, chunk_id)` checked against the allowlist.
- **Status:** COMPLETE.

### Phase 2C — Multi-paper comparison and AI relationships

- **Objective:** compare two of the owner's papers, first deterministically, then
  with an AI relationship layer that cites both sides.
- **Schema:** `migrations/0006_paper_relationship.sql` (applied), which enforces
  the canonical pair ordering `paper_a_id < paper_b_id` and records
  `paper_a_generated_at` / `paper_b_generated_at` for staleness detection.
- **Evidence:** commits `7398e3b`, `0c40249`, `6d08a3a`, `85c04e7`, `73265b0`,
  `be5c05f`, `0cc4be1`, `b0edbed`, `7a72802`;
  `tests/test_phase2c_relationship_schema.py`,
  `tests/test_phase2c_relationship_evidence.py`,
  `tests/test_phase2c_relationship_persistence.py`,
  `tests/test_phase2c_relationship_api.py`.
- **Status:** COMPLETE.

### Phase 3.2 — Verified Evidence Spans

- **Objective:** let an evidence entry carry an optional `quote` that is an exact
  span of the chunk it cites, refused outright rather than repaired if it is not.
- **Implemented:** three deterministic gates — blank, over `MAX_QUOTE_CHARS`
  (200), and not an exact substring of the one cited chunk. `schema_version`
  became `"2"`. The prompt's rule 8 was rewritten to request an optional verbatim
  span; the completion budget was deliberately held at 3000 at the time (see
  Phase 3.5 for why that was wrong).
- **Evidence:** commits `f552578`, `075c8a9`; `tests/test_phase3_evidence_spans.py`
  — note the filename says `phase3` while its docstring says "Phase 3.2". The
  mismatch is recorded rather than renamed.
- **Status:** COMPLETE.

### Phase 3.3 — Evidence Inspector UI

- **Objective:** cover the span-rendering behaviour of the Evidence Inspector,
  which had been span-capable since Phase 2B step 5 (`12ff9d3`) but had never
  received a span from production.
- **Implemented:** test coverage only. No frontend production code changed, and
  the UI deliberately does not branch on `schema_version`.
- **Evidence:** `src/components/paper-evidence-spans.test.tsx`.
- **Status:** COMPLETE.

### Phase 3.4 — Production QA (activity record, not a code artifact)

- **What it was:** a controlled production QA pass after Phase 3.3.
- **Finding:** one generation returned `schema_version: 2` with **zero** quotes
  across its evidence items. That finding is what motivated Phase 3.5.
- **No durable repository artifact exists.** There is no commit, test file,
  migration or source label for Phase 3.4 — a search of `backend/`,
  `research-compass-main/src` and `docs/` returns zero matches for the label.
  It is recorded here as a verification activity so the numbering is not
  mysterious, and **not** as an implementation.
- **Status:** ACTIVITY COMPLETE; no artifact.

### Phase 3.5 — Quote observability and completion-budget hardening

- **Objective:** make the quote pipeline diagnosable, then fix what the diagnosis
  exposed — without relaxing validation.
- **Implemented:**
  1. **`QuoteTally`** — six integer counters (`offered`, `kept`, `absent`,
     `dropped_blank`, `dropped_too_long`, `dropped_not_verbatim`) incremented
     inside the three existing gates, changing no validation decision. Counts are
     emitted in two existing server log lines and are **not** exposed through any
     API response. No raw quote, chunk, prompt or provider text is logged.
  2. **Completion budget raised 3000 → 5000.** The old number was justified by the
     prompt suppressing the `quote` field; Phase 3.2 inverted that rule and left
     the number alone. Production then returned `finish_reason=length` with
     `reasoning_tokens=1716`, leaving 1284 of 3000 for a body that needed more.
     5000 is `REPORT_MAX_TOKENS`, already proven against `GROQ_TIMEOUT_SECONDS`.
     `RELATIONSHIP_MAX_TOKENS` stays at 3000 — that object carries no `quote`
     field.
- **Evidence:** commits `f8804b9`, `ddd6818`;
  `tests/test_phase35_quote_observability.py` (35 tests),
  `tests/test_phase35_intelligence_budget.py` (24 tests). Exact-substring quote
  validation, `MAX_QUOTE_CHARS = 200`, the prompt and the schema are all unchanged
  — the prompt was verified byte-identical by AST source-segment hash.
- **Production verification (one controlled generation):** `finish_reason=stop` at
  the **same** `reasoning_tokens=1716` that had previously truncated under 3000;
  10 sections, 10 answered, 16 evidence items, `schema_version=2`, `generated_at`
  changed, result read back successfully. Telemetry: `offered=16 kept=1 absent=0
  blank=0 too_long=2 not_verbatim=13`. `(page, chunk_id)` remained the
  authoritative evidence identity, and every dropped span kept its reference.
- **Validation:** 38 Phase 3.2 tests, 35 Phase 3.5 observability tests, 24 budget
  tests, and the full backend suite at **1292 tests, 0 failed, 7 skipped**.
- **Status:** COMPLETE / CLOSED.

#### Phase 3.5 quote-quality investigation — measured findings

A read-only local experiment ran the repository's own
`pdf_loader → clean_text → create_chunks` path over the three PDFs already in
`backend/uploads/papers/`, and characterised the spans a model would be asked to
copy byte-exactly:

- 1,933 candidate spans (40–200 characters) across 230 chunks.
- **89 (4.6%)** contain neither a newline nor a non-ASCII character — the ceiling
  for a byte-exact copier.
- A newline is implicated in **96.3%** of breakable candidates and is the sole
  breaker in **56.8%**.
- Model-style normalization (newline→space, NFKC, curly quotes, dashes, space
  collapsing) recovered **no** additional matches.
- Production keep rate was **1/16 = 6.2%**, consistent with that 4.6% ceiling.

`clean_text` deliberately preserves newlines, and no stage normalises PDF
presentation artifacts such as the `ﬁ`/`ﬂ` ligatures, curly quotes and en dashes
the extraction produces.

**Not proven, and not claimed:** that the 13 production `not_verbatim` rejections
were newline failures. Those strings were never logged and cannot be
reconstructed, so no causal mapping from the local corpus to them was observed.
The validator is not implicated — its substring test cannot reject a true
substring, and that is covered by tests and mutation runs.

---

## 3. Current state

- **No defined major roadmap phase remains.** C1 was the last item defined as a
  major phase; the work in §2b landed afterwards in smaller approved steps, and
  Phase 3.5 is CLOSED.
- **Git:** `HEAD` = `ddd6818` (`fix: raise paper intelligence completion budget`),
  parent `f8804b9`. `origin/master` = `ddd6818`; local, tracking and server refs
  agree. The working tree was clean before this documentation update; while it is
  uncommitted, this roadmap file is the only working-tree modification. `e144ab0`,
  `cebd105` and `25064eb` are all ancestors of `HEAD` and pushed.
- **Migrations at `HEAD`:** six, all applied —
  `0001_core_schema.sql`, `0002_storage_policies.sql`, `0003_chat_messages.sql`,
  `0004_usage_counters.sql`, `0005_paper_intelligence.sql`,
  `0006_paper_relationship.sql`. There is no `0007`.
- **Application role:** `researchmind_app`, `rolbypassrls = false`.
- **RLS:** enabled and forced on the core tables; owner-only policies, unchanged,
  no explicit `WITH CHECK`.
- **Validation baseline — HISTORICAL, as of 2026-09-14. Not current:**

  | papers | reports | chat_sessions | chat_messages | Qdrant `researchmind_v2` |
  |---|---|---|---|---|
  | 3 | 0 | 1 | 6 | 220 |

  These were the counts when this section was first written. They are **known to
  be stale** — at minimum `reports` and `paper_intelligence` rows now exist from
  later work and from production QA. **Current values are unverified**: refreshing
  them requires a separate, separately approved read-only database verification,
  and no such query was run for this reconciliation. Note also that under forced
  RLS a `SELECT COUNT(*)` as `researchmind_app` with no JWT claim bound returns
  zero rows for every table, so any refresh must use `pg_stat_user_tables` or run
  with claims bound.

### Deployment status — DEPLOYED

The former "NOT DEPLOYED" statement in this section, and its comparison against
`origin/master` at `d83a16f`, are **superseded**. `d83a16f` is now an ancestor of
`HEAD`; `origin/master` is `ddd6818`; the product is deployed and serving.

- **Backend:** Railway. `backend/railway.toml` is the deployment configuration in
  the repository — `builder = "NIXPACKS"`, `startCommand = uvicorn app.main:app`,
  `healthcheckPath = "/health"`. `GET /health` returns HTTP 200 with
  `Server: railway-hikari`.
- **Deployment is still not a defined roadmap phase.** It is recorded here as
  current operational state. Any further deployment change needs its own approved
  plan.

**The deployed Git SHA has never been directly verified.** This limitation is
recorded precisely rather than glossed:

- The backend **exposes no build or commit identifier**. `GET /health` returns a
  hard-coded `"version": "3.1.0"` that has not changed since the initial commit,
  so it is constant across every revision. No Railway response header carries a
  revision or deployment id.
- The active deployment was **indirectly identified** from the Railway dashboard's
  deployment **commit message**, `fix: raise paper intelligence completion
  budget`, which `git log --all` shows belongs to exactly one commit, `ddd6818`.
  That is a one-to-one message↔commit mapping, **not** a read of the SHA itself.
- Neither the Railway CLI nor an authenticated session is available from the
  development environment, so deployment could not be verified programmatically.
- Consequence: this absent build identifier blocked deployment verification across
  four separate gates. Adding one would make future verification a single
  unauthenticated request. It is **not** a defined phase and is listed only as
  technical debt (§7 item 12).

**Stale `render.yaml` — documented, not modified.** The repository still contains a
root `render.yaml` naming a Render service `researchmind-api` with
**`autoDeploy: true`**, declaring only `GROQ_API_KEY`, `QDRANT_URL` and
`QDRANT_API_KEY`. It does not describe the live deployment, which is Railway. A
second auto-deploy configuration pointing at a different platform is an
operational hazard as well as a documentation one, and it requires **separate
review**; it was deliberately left untouched by this reconciliation. See §7
item 13.

---

## 4. C1 — RLS Hardening: design record (COMPLETE)

### Goal

Make Postgres row-level security an actual enforcement layer rather than a
decorative one. **Achieved** — see §2.

### Why it was needed

Before C1, RLS was **inert**, and this was measured, not assumed:

- All four tables reported `relrowsecurity = true`, `relforcerowsecurity = false`.
- Owner-only policies existed on all four (`<table>_owner_only`, `USING (owner_id = auth.uid())`).
- The application connected as role `postgres`, which **owned the tables** and had
  **`rolbypassrls = true`**.

Table owners bypass RLS unless `FORCE` is set, and a `BYPASSRLS` role bypasses it
regardless. So the policies never executed. **The only thing preventing
cross-tenant access before C1 was the explicit `WHERE owner_id = :owner_id` in
application code** — a single layer, where one missed filter in a future
endpoint would have been a data leak with nothing behind it.

### The four required changes — all landed

**a. A non-`BYPASSRLS` database role.** `researchmind_app` — does not own the
tables, does not carry `BYPASSRLS`, holds only the needed DML.

**b. `FORCE ROW LEVEL SECURITY`.** Set on all four tables in Stage 5. In practice
enforcement for the app began at Stage 4, because `researchmind_app` never owned
the tables; FORCE additionally constrains the owner, and would matter if
`postgres` ever lost `BYPASSRLS`.

**c. JWT claims into Postgres.** `auth.uid()` reads `request.jwt.claims`, set on
every transaction from the verified `owner_id` in `app/core/auth.py`.

**d. `SET LOCAL` discipline for pooled connections.** Claims are set with
`set_config(..., true)` inside the transaction and discarded on commit or
rollback. A plain `SET` was proven to persist across statements on the session
pooler, which is why it is forbidden by a source-level test.

### Dependencies

D2 complete (met). Required a Supabase role and a local `DATABASE_URL` change,
both done.

---

## 5. C1 safety and approval gates — as executed

**C1 was executed in staged, separately approved steps. No production write ran
without explicit approval for that specific step.**

| Stage | Action | Risk | Rollback | Outcome |
|---|---|---|---|---|
| 1 | **Read-only audit** — current roles, grants, policy definitions, `auth.uid()` behaviour under the pooler | None | n/a | PASS |
| 2 | **Role creation / grants** — new non-`BYPASSRLS` role; do *not* switch `DATABASE_URL` yet | Low | `DROP OWNED BY researchmind_app; DROP ROLE researchmind_app;` | PASS |
| 3 | **Claims plumbing** — `SET LOCAL` per request, still on the bypassing role so policies stay inert | Medium — must prove no leakage across pooled connections | Revert code; no DB change to undo | PASS |
| 4 | **Switch `DATABASE_URL`** to the new role | High — a missing grant locks the app out | Restore previous value | PASS |
| 5 | **`FORCE ROW LEVEL SECURITY`** — last | High — a wrong policy makes data invisible to its owner | `ALTER TABLE … NO FORCE ROW LEVEL SECURITY` | PASS |

Additional gates, all honoured:

- Stage 3 included an explicit **cross-request leakage test** on a pooled
  connection: two different owners in sequence on the same physical connection,
  proving the second could not see the first's rows.
- Every stage stated its rollback **before** execution.
- The `WHERE owner_id = :owner_id` filters were preserved throughout. RLS is
  defence in depth, **not** a replacement for them.
- No stage ran migrations, re-indexed, or altered a policy.
- Stage 5 ran the write matrix **before** FORCE as well as after, so a grant
  problem could not be confused with a FORCE problem.

---

## 6. Major phase count

**No defined major roadmap phase remains.** Count: **0**.

C1 was the last item defined in this project as a *major phase*. The work in §2b
landed after it in smaller separately approved steps, the most recent being Phase
3.5, which is CLOSED. Re-checked on 2026-09-27 against the repository: the strings
`Phase 4` and `Phase 5` appear **zero** times outside this document — in
`backend/app`, `backend/tests`, `backend/migrations`, `research-compass-main/src`,
or anywhere in `docs` other than `MASTER_ROADMAP.md` itself; the only "next phase"
text anywhere is this document's own Summary row; and there are **no**
`TODO`/`FIXME`/`XXX`/`HACK` comments in application or frontend source. So there is
not even an informal backlog that could be mistaken for a defined phase.

None are invented here. Items in sections 7 and 8 are a backlog, not phases;
deployment (§3) is current operational state, not a defined phase; and the
quote-quality directions in §9 are future work requiring a product decision, not a
phase.

No completion percentage is given: with no original roadmap document there is no
denominator, and inventing one would be fabrication.

---

## 7. Optional technical debt (13 items, 2 resolved)

Re-checked on 2026-09-27. Items 4 and 5 were confirmed still open by direct
inspection; item 5's line number had drifted. Items 12 and 13 are new and are
referenced from §3.

| # | Item | Location |
|---|---|---|
| 1 | `UserMemoryStore` has **no eviction** — `get`/`clear`/`owner_count` only; the dict grows unbounded per owner | `app/memory.py` |
| 2 | `last_research_query/report/context/sources` and `last_citations` are now **write-only** — D2 step 3 removed export's reads while `research.py` still writes them | `app/memory.py`, `app/api/research.py` |
| 3 | Dead SSE `"sources"` field duplicating `"citations"` in the `done` event | `app/api/ask_stream.py:325` |
| 4 | 8 debug `print()` calls, including the user's query, written to stdout. **Confirmed still open 2026-09-27** — exactly 8 `print(` remain, and line 73 is `print(f"Query: {query}")` | `app/api/research.py` |
| 5 | Hardcoded `"researchmind"` collection label (actual collection is `researchmind_v2`). **Confirmed still open 2026-09-27**; the line has drifted from 79 to **83** | `src/routes/dashboard.tsx:83` |
| 6 | ~~Two stale PDFs from June left in the working directory~~ **Resolved 2026-09-14** — deleted in the local generated-artifact cleanup (ignored files, never committed) | `backend/` |
| 7 | ~~Older `docs/` files stale, empty, or contradictory~~ **Resolved 2026-09-14** — `ROADMAP.md`, `ARCHITECTURE.md`, `API_REFERENCE.md` and `PROJECT_CONTEXT.md` deleted (see the header note) | `docs/` |
| 8 | Reranking disabled by default (`RERANK_ENABLED=false`) to keep memory low; retrieval quality is unreranked | `app/rag/reranker.py` |
| 9 | `GRANT USAGE ON SCHEMA auth TO researchmind_app` **granted nothing** — `postgres` does not own schema `auth`. Policies still evaluate `auth.uid()` correctly; only a *direct* `SELECT auth.uid()` by the app role is denied | Supabase `auth` schema |
| 10 | Policies rely on the **implicit** `WITH CHECK` (Postgres reuses `USING` for writes). Correct today, but an edit to `USING` alone would silently change write rules. Explicit `WITH CHECK` (5b) was deferred | migrations 0001, 0003 |
| 11 | **Secret hygiene — PARTLY ADDRESSED.** Originally: the `researchmind_app` password and two copies of the `postgres` password existed in a session scratchpad (outside the repository, never committed, absent from git history and transcripts), and the repository sits under the OneDrive root so `backend/.env` may be cloud-synced. A credential rotation **was** carried out in a later security-incident response, after this item was written. Marked partly addressed rather than resolved: the rotation is not verifiable from the repository, and the OneDrive-sync exposure of `backend/.env` is unchanged | local only |
| 12 | **The backend exposes no build or commit identifier.** `GET /health` returns a hard-coded `"version": "3.1.0"`, unchanged since the initial commit, and no response header carries a revision. The deployed SHA therefore cannot be verified by request — this blocked deployment verification across four gates (§3). Not a defined phase | `app/main.py` health endpoint |
| 13 | **Stale `render.yaml` with `autoDeploy: true`.** Names a Render service while the live deployment is Railway (`backend/railway.toml`), and declares only 3 of the environment variables the product needs. A second auto-deploy configuration for a different platform is an operational hazard; **requires separate review** and was deliberately not modified during the 2026-09-27 documentation reconciliation | `render.yaml` |

---

## 8. Known pre-existing bugs (9 items — 2 resolved, 1 with resolution evidence, 1 unverified)

Reconciled 2026-09-27. Items 1 and 5 were fixed by later work; item 8 has
resolution evidence but was not exhaustively re-verified; item 2 was **not**
verified during the reconciliation and is marked as such rather than guessed at.
The remaining items were not re-tested and are carried forward unchanged.

| # | Bug | Impact |
|---|---|---|
| 1 | ~~CORS entry `"https://*.vercel.app"` **never matches**~~ **RESOLVED** — `main.py` now uses `allow_origins=get_allowed_origins()` from `app/core/cors.py`, with optional `CORS_EXTRA_ORIGINS`. Commit `7dd86f0`, `tests/test_cors_config.py` | Was a blocker for preview-URL deployments; no longer applies |
| 2 | `/export-report` returns **HTTP 200 with a JSON error body** when no report exists — **UNVERIFIED as of 2026-09-27.** `app/api/export_report.py` exists and a `/latest-report` endpoint was added later (`f04f672`, `tests/test_latest_report.py`), but the no-report status code was **not** re-checked during reconciliation. Neither resolved nor unresolved is claimed | If still present: `response.ok` is true, so the browser saves a JSON file named `research-report.pdf`. A `404` would fix it |
| 3 | Papers with `status='failed'` are **invisible in the UI** — 2 of 3 current rows | Users cannot see or retry failed uploads |
| 4 | `FOLLOW_UP_WORDS` matching is **substring-based** — "net**work**s" contains "work" | Ordinary questions misread as follow-ups, silently prepending a stale topic |
| 5 | ~~`research.py` returns **raw exception strings** to the client~~ **RESOLVED** — the client-facing path now goes through `classify_provider_error()` and `internal_error_payload()`. A `print(f"Research Error: {str(e)}")` remains at `research.py:344`, which is a **server-side log**, not client exposure (and overlaps §7 item 4). Commit `8a49ea9`, `tests/test_internal_error_sanitization.py` | Client-facing internal-detail disclosure closed |
| 6 | `eslint .` fails with **917 errors** (843 are CRLF/prettier line-ending issues) | Pre-existing repo-wide; files untouched by recent work also fail |
| 7 | **No token revocation** | A signed-out user's JWT remains valid until expiry |
| 8 | Single-paper answer lock — **RESOLUTION EVIDENCE.** Commit `9d63bb1` ("allow new Ask questions to span multiple papers") with `tests/test_e2_multi_paper_retrieval.py`, and `3efbda1` with `tests/test_e2b_evidence_diversity.py`, address exactly this. Recorded as resolution evidence rather than confirmed-resolved: the behaviour was **not** exhaustively re-verified during reconciliation | Questions spanning two papers were previously unanswerable |
| 9 | Ingestion noise — roughly 6.1% of chunks contain line-number artifacts | Would require a re-index to correct |

---

## 9. Deferred / out of scope

**Major roadmap work:** none defined. C1 was the last major phase (§2, §4); the
later work in §2b is complete, with Phase 3.5 CLOSED.

**Deployment:** **now deployed** on Railway (§3), and still **not** a defined
phase — it is current operational state. Any further deployment change requires
its own separately approved plan.

**Optional cleanup:** 11 open items of 13 in section 7 (items 6 and 7 resolved,
item 11 partly addressed). None blocking.

**Known bugs:** the 9 items in section 8 — items 1 and 5 resolved, item 8 with
resolution evidence, item 2 unverified, the rest carried forward untested. Several
are one-line fixes.

**Quote-quality improvements — FUTURE WORK, not a phase.** Phase 3.5 measured a
~4.6% structural ceiling for byte-exact span copying and a 6.2% production keep
rate (§2b). The directions the evidence supports investigating are listed below.
They are **not ranked**, **none is selected**, and none is a defined phase; taking
any of them up is a product decision this document does not make:

- bounded normalized comparison while preserving the original chunk's text;
- improved quote-selection instructions in the generation prompt;
- richer quote telemetry, such as persisting or aggregating tallies;
- Unicode-variation span tests — ligature, curly quote, dash — the one coverage
  gap identified in Phase 3.5, given that 42.1% of locally measured candidate
  spans contain non-ASCII characters;
- treating the quote as an optional bonus while `(page, chunk_id)` remains the
  authoritative citation, which is the system's current behaviour;
- normalizing representation before indexing, which would require a re-index and
  is constrained by project rule 2.

**Explicitly not part of D2 — not defects:**

- **UI transcript restoration after refresh.** D2 delivered the persistence
  *layer*. There is no chat-history read endpoint, and `src/routes/ask.tsx` holds
  the transcript in plain `useState`. **The transcript clearing on refresh is
  expected behaviour, not a regression.** The durable state is still present and
  still drives server-side behaviour — verified in D2 and again in C1 Part 2,
  where a post-refresh follow-up remained correctly grounded. Surfacing history
  in the UI would need a new endpoint plus UI work, and was never in scope.
- **Concurrency proof for the `UNIQUE(owner_id)` race.** The constraint, the
  `UniqueViolation` and the retry recovery were all verified against real
  Postgres, but the two attempts were forced sequentially rather than raced
  across connections.

**Not performed in C1:**

- **Owner B browser verification.** Cross-owner isolation was verified at the
  database layer (Stages 3–5, W1–W12) and the endpoint layer (Part 1a E8), not in
  a second-account browser session.
- **Adversarial or penetration testing.** Isolation is enforced at two layers and
  was tested with deliberate cross-owner reads and writes, but no adversarial
  testing was carried out.

---

## 10. Project rules and safety

These rules were established over the course of the project and remain in force.

### Operational

1. **No destructive operation without explicit approval** — deletes, overwrites,
   drops, force-pushes, or anything hard to reverse.
2. **No Qdrant re-indexing without approval.** The collection currently holds 220
   points for one indexed paper; re-indexing costs Voyage quota and time.
3. **No Voyage API key or model changes without approval.** The free tier is
   3 RPM / 10K TPM; the embedding path is deliberately paced and backed off.
4. **No production schema, RLS, or role changes without approval.** Migrations
   are additive, guarded, reviewed as SQL before execution, and never run
   automatically by the application.
5. **Secrets are never printed.** `.env` / `.env.local` are never read for display
   and never modified without explicit, narrowly-scoped approval.
6. **Work in staged steps:** plan → approval → small change → test → report →
   stop. Live and costly operations are approved individually.

### Security invariants — do not weaken

7. **`owner_id` comes only from the verified JWT `sub`**, derived in exactly one
   place (`app/core/auth.py`).
8. **Server-side tenant isolation.** Every database query and every Qdrant
   operation carries a server-constructed owner filter. There is no code path
   that lets a caller widen or omit it.
9. **No client-controlled `owner_id`.** No endpoint accepts an owner identifier
   from a query parameter, body, or header. Tests assert this explicitly.
10. **No authentication bypass in production.** Dependency overrides exist only
    inside the test suite. Any verification that bypasses JWT verification must
    say so plainly in its report.
11. **The evidence invariant:** the exact passages supplied to the model are the
    exact passages shown as Sources. Enforced in code by a single `selected`
    list, not by prompt instruction.
12. **Tests must be deterministic and offline.** Anything contacting a real
    service is gated behind `RUN_LIVE_INFRA_TESTS=1` and skips by declaration,
    never by a swallowed connection error.
13. **The application connects as `researchmind_app`, never `postgres`.**
    `postgres` is reserved for migrations and administration.
14. **Database access goes through `session_scope(owner_id)` or
    `get_db_session`** so every transaction carries claims. Claims are set only
    with `set_config(..., true)`; a plain session-level `SET` is forbidden and
    guarded by a source-level test.

### Deployment safety

15. **Never push to `master` without an approved deployment plan. A push is a
    deployment.** The live backend is on **Railway**, configured by
    `backend/railway.toml`, and it builds from this repository. The stale root
    `render.yaml` **also** still sets `autoDeploy: true` for a Render service
    (§7 item 13), so two platforms are nominally armed against `master`; that
    needs separate review. Corrected 2026-09-27 — this rule previously named only
    Render.
16. **The deployed revision cannot be confirmed by request.** The backend exposes
    no build identifier (§7 item 12), so a deployment claim must either cite the
    Railway dashboard or say plainly that the SHA was not directly verified. Do
    not infer a deployed commit from a clean tree, matching git refs, a healthy
    `/health`, or the fact that auto-deploy usually works.

---

## Summary

| | |
|---|---|
| **Last completed work** | Phase 3.5 — Quote observability and completion-budget hardening: **COMPLETE / CLOSED** |
| **Last defined major phase** | C1 — RLS Hardening: **COMPLETE / PASS** |
| **Next phase** | **None defined** |
| **Major phases remaining** | **0** |
| **Git `HEAD`** | `ddd6818` — `origin/master` matches; tree was clean before this documentation update, which is the only working-tree modification until committed |
| **Migrations** | 6 applied (`0001`–`0006`); no `0007` |
| **Deployment** | **DEPLOYED** — Railway, `backend/railway.toml`, `/health` returns 200. Deployed SHA **not directly verified**: no build identifier exists, so `ddd6818` was identified only indirectly, via the deployment's commit message |
| **Validation baseline** | **HISTORICAL (2026-09-14)** — papers 3 · reports 0 · chat_sessions 1 · chat_messages 6 · Qdrant 220. **Current values unverified**; refreshing them needs a separate approved read-only database check |
| **Test suite** | 1292 tests, 0 failed, 7 skipped |
| **Optional cleanup items** | 11 open of 13 (2 resolved, 1 partly addressed) |
| **Known pre-existing bugs** | 9 listed — 2 resolved, 1 with resolution evidence, 1 unverified |
| **Blockers** | None identified for the current product. Outstanding operational concerns: the stale `render.yaml` with `autoDeploy: true` on a second platform, and the absent build identifier that prevents deployed-SHA verification. |
