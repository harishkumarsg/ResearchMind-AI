# ResearchMind AI — Master Project Roadmap

**Last updated:** 2026-09-13
**Status of this document:** current and authoritative.

> **This supersedes `docs/ROADMAP.md` as the project roadmap.**
> `docs/ROADMAP.md` is unchanged since the initial commit and describes a
> pre-security feature list that no longer reflects the project. It has been
> left in place deliberately, not updated. `docs/PROJECT_CONTEXT.md` is also
> stale (it still names Sentence Transformers / Cross Encoder / Ollama, all of
> which were replaced). `docs/ARCHITECTURE.md` is empty.
>
> Every claim below is tied to a durable artifact — a test file, a migration,
> a code location, or a recorded verification run. Where something could not be
> verified, it says so rather than guessing.

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
FastAPI backend  ← the only authorization boundary
   ├── app/core/auth.py      JWT verified via JWKS; owner_id = verified `sub`
   ├── app/api/*             endpoints; every query filtered by owner_id
   ├── app/services/         storage.py (Supabase Storage), chat_store.py
   ├── app/rag/              embedder (Voyage), vector_store (Qdrant), reranker
   └── app/db/               SQLAlchemy models + session factory
   │
   ├── Supabase Postgres     papers, reports, chat_sessions, chat_messages
   ├── Supabase Storage      bucket "papers", path papers/{owner}/{paper}/original.pdf
   ├── Qdrant Cloud          collection researchmind_v2
   ├── Voyage AI             embeddings + reranking
   └── Groq                  answer generation
```

**`owner_id` is derived in exactly one place** — `app/core/auth.py`, from the
verified JWT `sub`. No endpoint accepts an owner identifier from the client.

### Established production technology

| Layer | Choice | Notes |
|---|---|---|
| Backend | FastAPI 0.136.3 / Starlette 1.2.1, Python 3.13 | `render.yaml` → `uvicorn app.main:app` |
| ORM / driver | SQLAlchemy 2.0.50, psycopg 3.3.5 | `DATABASE_URL` uses `postgresql+psycopg://` |
| Database | Supabase Postgres **17.6** | session-mode pooler, port 5432 |
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

- **Status:** COMPLETE / PASS.

---

## 3. Current state

- **D2 = COMPLETE / PASS.**
- Real Google-authenticated browser QA **passed**.
- Final read-only Postgres verification **passed** — all checks.
- **6 real browser-QA chat rows intentionally remain** in the database
  (1 `chat_sessions`, 6 `chat_messages` for the QA owner). These are genuine
  authenticated user actions, not synthetic test data, and were deliberately not
  cleaned up.
- Row counts at time of writing: `papers` 3, `reports` 0, `chat_sessions` 1,
  `chat_messages` 6.
- **Local backend and frontend dev servers are stopped**; ports 8000 and 8080
  are free.
- Working tree contains uncommitted D1/D2 work; nothing has been committed or
  deployed as part of these phases.

---

## 4. Next major phase — C1: RLS Hardening

### Goal

Make Postgres row-level security an actual enforcement layer rather than a
decorative one.

### Why it is needed

RLS is currently **inert**, and this is measured, not assumed:

- All four tables report `relrowsecurity = true`, `relforcerowsecurity = false`.
- Owner-only policies exist on all four (`<table>_owner_only`, `USING (owner_id = auth.uid())`).
- The application connects as role `postgres`, which **owns the tables** and has
  **`rolbypassrls = true`**.

Table owners bypass RLS unless `FORCE` is set, and a `BYPASSRLS` role bypasses it
regardless. So the policies never execute. **The only thing preventing cross-tenant
access today is the explicit `WHERE owner_id = :owner_id` in application code.**
That has been tested extensively and holds — but it is a single layer, and a
single missed filter in a future endpoint would be a data leak with nothing
behind it.

### The four required changes

These must land **together**; any one alone leaves the system broken or still
unprotected.

**a. A non-`BYPASSRLS` database role.** Create a role that does not own the
tables and does not carry `BYPASSRLS`, grant it only the needed DML, and point
`DATABASE_URL` at it.

**b. `FORCE ROW LEVEL SECURITY`.** Without this, a table owner still bypasses
policies. Deliberately not set by migration 0003.

**c. JWT claims into Postgres.** Policies call `auth.uid()`, which reads a
request-scoped setting. The backend must pass the verified JWT claims into the
database session on every request, derived from the same single source in
`app/core/auth.py`.

**d. `SET LOCAL` discipline for pooled connections.** Claims must be set with
`SET LOCAL` inside a transaction so they are discarded on commit or rollback.
The app connects through Supabase's **session-mode pooler**, so a plain `SET`
would persist on a reused connection and leak one user's identity into the next
request. **This is the highest-risk item in the phase.**

### Dependencies

D2 complete (met). Requires a Supabase role change and a `DATABASE_URL` change.
No application feature work depends on C1, and C1 depends on no other phase.

---

## 5. C1 safety and approval gates

**C1 must be executed in staged, separately approved steps. No production write
without explicit approval for that specific step.**

| Stage | Action | Risk | Rollback |
|---|---|---|---|
| 1 | **Read-only audit** — current roles, grants, policy definitions, `auth.uid()` behaviour under the pooler | None | n/a |
| 2 | **Role creation / grants** — new non-`BYPASSRLS` role; do *not* switch `DATABASE_URL` yet | Low | `DROP ROLE`; app still on the old role |
| 3 | **Claims plumbing** — `SET LOCAL` per request, still on the bypassing role so policies stay inert | Medium — must prove no leakage across pooled connections | Revert code; no DB change to undo |
| 4 | **Switch `DATABASE_URL`** to the new role | High — a missing grant locks the app out | Restore previous value |
| 5 | **`FORCE ROW LEVEL SECURITY`** — last | High — a wrong policy makes data invisible to its owner | `ALTER TABLE … NO FORCE ROW LEVEL SECURITY` |

Additional gates:

- Stage 3 must include an explicit **cross-request leakage test** on a pooled
  connection: two different owners in sequence on the same physical connection,
  proving the second cannot see the first's rows.
- Every stage states its rollback **before** execution.
- Preserve the `WHERE owner_id = :owner_id` filters throughout. RLS is defence in
  depth, **not** a replacement for them; removing them is out of scope.
- No step may run migrations, re-index, or touch Storage/Qdrant/Voyage/Groq.

---

## 6. Major phase count

**Exactly 1 defined major phase remains: C1.**

No further phases are defined in this project. None are invented here. Items in
sections 7 and 8 are a backlog, not phases.

No completion percentage is given: with no original roadmap document there is no
denominator, and inventing one would be fabrication.

---

## 7. Optional technical debt (8 items)

| # | Item | Location |
|---|---|---|
| 1 | `UserMemoryStore` has **no eviction** — `get`/`clear`/`owner_count` only; the dict grows unbounded per owner | `app/memory.py` |
| 2 | `last_research_query/report/context/sources` and `last_citations` are now **write-only** — D2 step 3 removed export's reads while `research.py` still writes them | `app/memory.py`, `app/api/research.py` |
| 3 | Dead SSE `"sources"` field duplicating `"citations"` in the `done` event | `app/api/ask_stream.py:325` |
| 4 | 8 debug `print()` calls, including the user's query, written to stdout | `app/api/research.py` |
| 5 | Hardcoded `"researchmind"` collection label (actual collection is `researchmind_v2`) | `src/routes/dashboard.tsx:79` |
| 6 | Two stale PDFs from June left in the working directory | `backend/ResearchMind_Report.pdf`, `backend/research_report.pdf` |
| 7 | **All three older `docs/` files are stale or empty** — `ROADMAP.md` predates the security work, `PROJECT_CONTEXT.md` names a replaced stack, `ARCHITECTURE.md` is 0 bytes | `docs/` |
| 8 | Reranking disabled by default (`RERANK_ENABLED=false`) to keep memory low; retrieval quality is unreranked | `app/rag/reranker.py` |

---

## 8. Known pre-existing bugs (9 items)

| # | Bug | Impact |
|---|---|---|
| 1 | CORS entry `"https://*.vercel.app"` **never matches** — Starlette does exact origin matching, not globbing | Vercel preview deployments are blocked. Fails closed, so not a security hole |
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

**Major roadmap work:** C1 only (section 4).

**Optional cleanup:** the 8 items in section 7. None blocking.

**Known bugs:** the 9 items in section 8. None blocking; several are one-line fixes.

**Explicitly not part of D2 — not defects:**

- **UI transcript restoration after refresh.** D2 delivered the persistence
  *layer*. There is no chat-history read endpoint, and `src/routes/ask.tsx` holds
  the transcript in plain `useState`. **The transcript clearing on refresh is
  expected behaviour, not a regression.** The durable state is still present and
  still drives server-side behaviour — verified in browser QA, where a
  post-refresh follow-up remained correctly grounded. Surfacing history in the UI
  would need a new endpoint plus UI work, and was never in D2's scope.
- **Concurrency proof for the `UNIQUE(owner_id)` race.** The constraint, the
  `UniqueViolation` and the retry recovery were all verified against real
  Postgres, but the two attempts were forced sequentially rather than raced
  across connections.
- **Adversarial cross-tenant testing.** With RLS inert, isolation rests entirely
  on application filters. Proving enforcement under attack is C1 work.

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

---

## Summary

| | |
|---|---|
| **Current phase** | D2 — Durable Persistence: **COMPLETE / PASS** |
| **Next phase** | C1 — RLS Hardening |
| **Major phases remaining** | **1** |
| **Optional cleanup items** | 8 |
| **Known pre-existing bugs** | 9 |
| **Blockers** | None for C1. Requires a new Supabase role, a `DATABASE_URL` change, and `ALTER TABLE` — each separately approved. The `SET LOCAL` pooled-connection requirement is the principal hazard. |
