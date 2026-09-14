# ResearchMind AI — Master Project Roadmap

**Last updated:** 2026-09-14
**Status of this document:** current and authoritative.

> **This supersedes `docs/ROADMAP.md` as the project roadmap.**
> `docs/ROADMAP.md` is unchanged since the initial commit and describes a
> pre-security feature list that no longer reflects the project. It has been
> left in place deliberately, not updated. `docs/ARCHITECTURE.md` is empty.
>
> **`docs/PROJECT_CONTEXT.md` contradicts the current system and must not be
> used as guidance.** It names a replaced stack (Sentence Transformers, Cross
> Encoder, Ollama/Qwen), documents endpoints that no longer exist in that form
> (`GET /index-document` is now POST-only; `/ask` was deleted), describes local
> `uploads/papers/` storage with no authentication, and its coding rules include
> "Do not replace Ollama". Following it would regress the Phase 1 safety fixes
> and the Phase 1.5 authentication model.
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
   ├── Supabase Postgres     papers, reports, chat_sessions, chat_messages
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

This describes the locally validated product. **The deployed code differs
substantially; see §3.**

| Layer | Choice | Notes |
|---|---|---|
| Backend | FastAPI 0.136.3 / Starlette 1.2.1, Python 3.13 | `render.yaml` → `uvicorn app.main:app` |
| ORM / driver | SQLAlchemy 2.0.50, psycopg 3.3.5 | `DATABASE_URL` uses `postgresql+psycopg://` |
| Database | Supabase Postgres **17.6** | session-mode pooler, port 5432 |
| Database role | `researchmind_app` | NOBYPASSRLS, owns no tables; username `researchmind_app.<project_ref>` |
| Row-level security | enabled **and forced** on all four tables | owner-only policies, `USING (owner_id = auth.uid())` |
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

- **Committed:** local commit `cebd105`. **Not pushed, not deployed.**
- **Status:** COMPLETE / PASS.

---

## 3. Current state

- **C1 = COMPLETE / PASS. No defined major roadmap phase remains.**
- **Local git:** `HEAD` = `cebd105` (C1 claims plumbing), parent `e144ab0`
  (D0–D2 checkpoint). Both commits exist **locally only**.
- **Local application role:** `researchmind_app`, `rolbypassrls = false`.
- **RLS:** enabled and forced on all four tables; four owner-only policies,
  unchanged, no explicit `WITH CHECK`.
- **Local validation baseline** (shared Supabase project and Qdrant cluster):

  | papers | reports | chat_sessions | chat_messages | Qdrant `researchmind_v2` |
  |---|---|---|---|---|
  | 3 | 0 | 1 | 6 | 220 |

  All 220 points belong to paper `f8d48357-a77b-5d9e-a0c2-7ff24b25a580`
  (`status = indexed`). The one chat session and six messages are genuine D2
  browser-QA rows, intentionally retained. The other two `papers` rows have
  `status = failed` (see §8 item 3).

### Deployment status — NOT DEPLOYED

**The completed product exists only locally.** Local PASS is not deployment.

| | Local (`cebd105`) | `origin/master` (`d83a16f`) |
|---|---|---|
| Date | 2026-09-14 | 2026-06-24 |
| Authentication | Supabase JWT via JWKS | **none** |
| Tenant isolation | app filters + forced RLS | **none** |
| Postgres | `researchmind_app`, RLS forced | **not used** — never reads `DATABASE_URL` |
| Qdrant collection | `researchmind_v2` | `researchmind` |
| Environment variables read | 15 | 4 (`GROQ_API_KEY`, `QDRANT_URL`, `QDRANT_API_KEY`, `RERANK_ENABLED`) |

- `origin/master` is **pre-Phase-1.5** and is **not equivalent** to the local
  completed product. It lacks Phases 1.5, 2, D1, D2 and C1 entirely.
- Commits `e144ab0` and `cebd105` have **not** been pushed.
- `render.yaml` sets **`autoDeploy: true`** — pushing to `master` deploys.
- `render.yaml` declares only `GROQ_API_KEY`, `QDRANT_URL` and `QDRANT_API_KEY`.
  The local product also requires `DATABASE_URL`, `SUPABASE_URL`,
  `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY` and `VOYAGE_API_KEY`.
- Whether any service is currently live, which provider hosts it (`render.yaml`
  names Render; the last commit message names Railway), and which environment
  variables it holds are **not visible from the repository**.
- The forced RLS lives in the shared Supabase project. Deployed code at
  `d83a16f` never connects to Postgres, so it is unaffected by it.
- **Deployment is not a defined roadmap phase.** It requires its own separately
  approved plan.

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

C1 was the last phase defined in this project. None are invented here. Items in
sections 7 and 8 are a backlog, not phases, and deployment (§3) is not a defined
phase.

No completion percentage is given: with no original roadmap document there is no
denominator, and inventing one would be fabrication.

---

## 7. Optional technical debt (11 items)

| # | Item | Location |
|---|---|---|
| 1 | `UserMemoryStore` has **no eviction** — `get`/`clear`/`owner_count` only; the dict grows unbounded per owner | `app/memory.py` |
| 2 | `last_research_query/report/context/sources` and `last_citations` are now **write-only** — D2 step 3 removed export's reads while `research.py` still writes them | `app/memory.py`, `app/api/research.py` |
| 3 | Dead SSE `"sources"` field duplicating `"citations"` in the `done` event | `app/api/ask_stream.py:325` |
| 4 | 8 debug `print()` calls, including the user's query, written to stdout | `app/api/research.py` |
| 5 | Hardcoded `"researchmind"` collection label (actual collection is `researchmind_v2`) | `src/routes/dashboard.tsx:79` |
| 6 | Two stale PDFs from June left in the working directory | `backend/ResearchMind_Report.pdf`, `backend/research_report.pdf` |
| 7 | **All three older `docs/` files are stale, empty, or contradictory** — `ROADMAP.md` predates the security work, `ARCHITECTURE.md` is 0 bytes, and `PROJECT_CONTEXT.md` actively contradicts the current system (see the header note) | `docs/` |
| 8 | Reranking disabled by default (`RERANK_ENABLED=false`) to keep memory low; retrieval quality is unreranked | `app/rag/reranker.py` |
| 9 | `GRANT USAGE ON SCHEMA auth TO researchmind_app` **granted nothing** — `postgres` does not own schema `auth`. Policies still evaluate `auth.uid()` correctly; only a *direct* `SELECT auth.uid()` by the app role is denied | Supabase `auth` schema |
| 10 | Policies rely on the **implicit** `WITH CHECK` (Postgres reuses `USING` for writes). Correct today, but an edit to `USING` alone would silently change write rules. Explicit `WITH CHECK` (5b) was deferred | migrations 0001, 0003 |
| 11 | **Secret hygiene pending:** the `researchmind_app` password and two copies of the `postgres` password exist in a session scratchpad (outside the repository, never committed, absent from git history and transcripts); the repository sits under the OneDrive root, so `backend/.env` may be cloud-synced; rotation of both passwords is recommended before any deployment | local only |

---

## 8. Known pre-existing bugs (9 items)

| # | Bug | Impact |
|---|---|---|
| 1 | CORS entry `"https://*.vercel.app"` **never matches** — Starlette does exact origin matching, not globbing | Vercel preview deployments are blocked. Fails closed, so not a security hole — but a **blocker for any deployment** that uses preview URLs |
| 2 | `/export-report` returns **HTTP 200 with a JSON error body** when no report exists | `response.ok` is true, so the browser saves a JSON file named `research-report.pdf`. A `404` would fix it |
| 3 | Papers with `status='failed'` are **invisible in the UI** — 2 of 3 current rows | Users cannot see or retry failed uploads |
| 4 | `FOLLOW_UP_WORDS` matching is **substring-based** — "net**work**s" contains "work" | Ordinary questions misread as follow-ups, silently prepending a stale topic |
| 5 | `research.py` returns **raw exception strings** to the client | Possible internal detail disclosure |
| 6 | `eslint .` fails with **917 errors** (843 are CRLF/prettier line-ending issues) | Pre-existing repo-wide; files untouched by recent work also fail |
| 7 | **No token revocation** | A signed-out user's JWT remains valid until expiry |
| 8 | Single-paper answer lock | Questions spanning two papers cannot be answered |
| 9 | Ingestion noise — roughly 6.1% of chunks contain line-number artifacts | Would require a re-index to correct |

---

## 9. Deferred / out of scope

**Major roadmap work:** none defined. C1 is complete (§2, §4).

**Deployment:** not deployed and not a defined phase (§3). Requires its own
separately approved plan.

**Optional cleanup:** the 11 items in section 7. None blocking the local product.

**Known bugs:** the 9 items in section 8. None blocking the local product;
several are one-line fixes. Item 1 blocks preview deployments.

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

15. **Never push to `master` without an approved deployment plan.** `render.yaml`
    sets `autoDeploy: true`, so a push is a deployment.

---

## Summary

| | |
|---|---|
| **Current phase** | C1 — RLS Hardening: **COMPLETE / PASS** |
| **Next phase** | **None defined** |
| **Major phases remaining** | **0** |
| **Local git `HEAD`** | `cebd105` — local only, not pushed |
| **Deployment** | **NOT DEPLOYED** — `origin/master` is `d83a16f`, pre-Phase-1.5, not equivalent to the local product |
| **Validation baseline** | papers 3 · reports 0 · chat_sessions 1 · chat_messages 6 · Qdrant 220 |
| **Optional cleanup items** | 11 |
| **Known pre-existing bugs** | 9 |
| **Blockers** | None for the local product. Any deployment needs its own approved plan: required environment variables, secret rotation, CORS fix, and control over `autoDeploy`. |
