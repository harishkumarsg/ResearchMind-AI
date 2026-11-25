# ResearchMind AI

[![Tests](https://github.com/harishkumarsg/ResearchMind-AI/actions/workflows/tests.yml/badge.svg)](https://github.com/harishkumarsg/ResearchMind-AI/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

A private research workspace for academic PDFs. You upload papers, they are indexed into a vector database, and you can search them, ask questions with answers grounded in cited passages, compare papers, and export reports. Every query is scoped to the signed-in user's own library.

> **Live app:** [www.researchmindlab.com](https://www.researchmindlab.com)
> Status: personal project, built and maintained by Harish Kumar S G (solo developer, AI-assisted; see [Provenance](#provenance)).

![ResearchMind AI: a question answered with cited source passages](docs/screenshot.png)

---

## What it does

| Feature | Where it lives |
|---|---|
| PDF upload to per-user storage | `backend/app/api/upload.py`, `services/storage.py` |
| Page-aware extraction, chunking and indexing | `services/pdf_loader.py`, `rag/chunker.py`, `api/index_document.py` |
| Semantic search over your library | `api/search.py`, `rag/vector_store.py` |
| Streamed Q&A with cited source passages, multi-paper | `api/ask_stream.py` |
| Structured "paper intelligence" with quote validation | `api/paper_intelligence.py`, `services/intelligence_schema.py` |
| Cross-paper relationship analysis | `api/paper_relationship.py`, `services/relationship_schema.py` |
| Paper comparison, summaries, report export (PDF) | `api/compare_papers.py`, `api/summarize_paper.py`, `api/export_report.py` |
| Per-user rate limits, daily quotas, per-paper cost caps | `core/rate_limit.py`, `core/quota.py`, `core/limits.py` |

## Architecture

```
Browser (React 19 + TanStack Start)
  │  Supabase Auth (Google OAuth) → JWT in Authorization header
  ▼
FastAPI backend — the only authorization boundary
  ├── core/auth.py      verifies the JWT via Supabase JWKS; owner_id = verified `sub`
  ├── api/*             every query filtered by owner_id
  └── db/               SQLAlchemy; verified claims set on every transaction
  │
  ├── Supabase Postgres  row-level security per owner (see migrations/)
  ├── Supabase Storage   {owner}/{paper}/original.pdf
  ├── Qdrant Cloud       vectors with owner + page payload
  ├── Voyage AI          embeddings (voyage-4), optional reranking (rerank-3)
  └── Groq               answer generation (gpt-oss-120b)
```

**Design decisions worth knowing**

- **Tenant isolation is enforced twice:** an explicit `owner_id` filter in every query, and Postgres row-level security driven by the same verified identity. `owner_id` is never accepted from the client.
- **Retrieval:** recursive character chunking (1,500 chars, 300 overlap); reference lists are not indexed; top-8 vector search, top 5 kept, up to 4 passages and 6,000 characters to the model. Voyage reranking is available behind `RERANK_ENABLED` but off by default (see [Retrieval evaluation](#retrieval-evaluation)).
- **Grounding:** answers are generated only from retrieved passages. In structured outputs, every evidence reference must point to a (page, chunk) pair that was actually supplied, or the whole object is rejected; a quote that is not an exact substring of the chunk it cites is dropped, never paraphrased or repaired. Drops are counted by reason (counts only, no text) so grounding failures stay visible.
- **Cost control:** quotas and per-paper caps are checked *before* any embedding or generation call.

## Deployment

| Component | Host |
|---|---|
| Backend (FastAPI) | Railway (`backend/railway.toml`) |
| Frontend (React) | Vercel |
| Auth, Postgres, file storage | Supabase |
| Vector search | Qdrant Cloud |

## Tests

1,595 backend tests (`unittest`) and 562 frontend tests (Vitest), counted from a local run on 2026-11-23. The backend suite runs fully offline: providers are mocked and persistence uses in-memory SQLite. Offline, 30 backend tests are skipped: 20 PostgreSQL lifecycle tests, 4 live-infrastructure tests, 3 deferred Qdrant acceptance tests, and 3 that need local PDFs or the Linux target platform.

```bash
cd backend
pip install -r requirements.txt
# Placeholder values only; no real keys are needed for the offline suite.
export GROQ_API_KEY=x VOYAGE_API_KEY=x QDRANT_URL=http://localhost:1 QDRANT_API_KEY=x \
       SUPABASE_URL=http://localhost:1 SUPABASE_ANON_KEY=x SUPABASE_SERVICE_ROLE_KEY=x
python -m unittest discover -s tests
```

Tests that need live infrastructure are skipped unless `RUN_LIVE_INFRA_TESTS=1`. Frontend: `cd research-compass-main && npm ci --legacy-peer-deps && npm test` (the flag works around a known peer-range conflict in a locked prerelease dependency; a proper lockfile fix is pending). Both suites run in CI (`.github/workflows/tests.yml`).

The PostgreSQL lifecycle tests (`backend/tests/test_paper_lifecycle_postgres.py`) check the paper claim protocol under real row locks, as the restricted app role with RLS forced. They need a disposable database and are skipped unless `RUN_POSTGRES_LIFECYCLE_TESTS=1` and `POSTGRES_LIFECYCLE_TEST_URL` are set. `.github/workflows/postgres-lifecycle.yml` runs them on every pull request against a throwaway `postgres:17` service container, migrated from `backend/migrations/`; it uses no repository secret and cannot reach Supabase.

## Retrieval evaluation

Retrieval only: for each question, does the retrieval path return passages from the right paper and page? Answer quality is not measured here. Everything is in [`backend/eval/`](backend/eval/README.md).

**Result.** Context hit is the share of answerable questions for which a gold page is among the passages handed to the model. On the held-out test set it was **‹before: to confirm›** with the production settings before this change and **96%** with the settings now in production: reference lists not indexed, reranking off, 6,000 characters of context.
<!-- "before" depends on whether RERANK_ENABLED was true in production (Railway):
     true  -> run baseline,   62% context hit
     false -> run rerank-off, 88% context hit
     Fill in once confirmed, and name the run. -->

**Setup.** Five papers on industrial anomaly detection (75 pages; listed in `backend/eval/README.md`, PDFs not in the repo). 64 questions with page-level gold, split 38 for development and 26 for test (stratified by question type). The test set has 6 factual, 6 method, 4 results, 4 limitation, 4 cross-paper and 2 unanswerable items, so metrics are over **24** answerable questions. `run_retrieval_eval.py` indexes the papers with the production indexer and runs every question through the production `/ask-stream` retrieval code, with Voyage results cached.

**Protocol.** All variants were designed and compared on the development set only. The test protocol was committed before any test run: the test file is frozen by SHA-256, each pre-registered run is made once, the script refuses to overwrite a test result, and every result is reported, including the ones that are worse than on dev. Nothing was tuned after the test results were seen.

**Test-set results** (24 answerable questions; R@k and MRR@5 on the list the context is taken from):

| Run | R@1 | R@3 | R@5 | MRR@5 | Context hit | Voyage requests per question |
|---|---|---|---|---|---|---|
| `baseline`: rerank on (vector top 5, first 400 characters), 4,000-char context | 58% | 75% | 92% | 0.683 | 62% | 2 |
| `rerank-off`: the code's default, 4,000-char context | 71% | 88% | 92% | 0.795 | 88% | 1 |
| `no-refs-ctx6000-rerank-off`: **now in production** | 75% | 92% | 96% | 0.837 | 96% | 1 |
| `no-refs-top20-rerank10-ctx6000`: vector top 20, rerank 10 on full text | 75% | 100% | 100% | 0.854 | 100% | 2 |

The last run scored highest, but it makes two Voyage requests per question instead of one, and its full-text rerank requests are large against the free tier's 10K tokens a minute. The run now in production needs one embedding request per question, which fits the free tier. Per-type numbers, cross-paper coverage and the questions that flipped are in [`backend/eval/results/compare-test.md`](backend/eval/results/compare-test.md).

**Implementation check.** After the change, the production code was run through the same harness from cached embeddings (`impl-check-dev`, `impl-check-test`). Every chunk was byte-identical to the measured index, and every ranking and metric matched `no-refs-ctx6000-rerank-off` on both sets. This confirms the code matches what was measured. It is not a new result.

**Limits.**
- Small: 24 answerable test questions over one five-paper corpus in one field. One question moves a rate by about 4 points.
- The questions and gold pages were drafted by an AI (Claude Code) and checked automatically against the PDF text. Five flagged items were decided by the author; not every item was re-read by a person.
- The questions usually name the paper or method they ask about, which makes retrieval easier than for real questions that do not.
- Retrieval only: whether the answer is correct and cites the right page is not measured.
- Vector search ran in Qdrant's local mode, which searches exactly. Qdrant Cloud should rank the same at this collection size, but this was not verified. Latencies are from a local machine, not production.
- The reranking result is for the reranking as configured here (top 5, first 400 characters of each chunk), not for reranking in general.

## Running locally

1. Create a Supabase project, enable Google OAuth, and a Qdrant Cloud cluster.
2. Apply `backend/migrations/bootstrap/app_role.sql`, then `0001` to `0007` in order. Set the app role's password out of band.
3. Copy `backend/.env.example` to `backend/.env` and `research-compass-main/.env.example` to `research-compass-main/.env`, and fill in your own values. Use Supabase's new API keys (Project Settings → API Keys): `SUPABASE_ANON_KEY` takes the publishable key (`sb_publishable_…`) and `SUPABASE_SERVICE_ROLE_KEY` a secret key (`sb_secret_…`). The names are historical; legacy JWT keys (`eyJ…`) are disabled on the production project, so any request made with one is rejected.
4. Backend: `cd backend && uvicorn app.main:app --reload` (API docs at `/docs`).
5. Frontend: `cd research-compass-main && npm ci --legacy-peer-deps && npm run dev`.

## Known limitations

- The retrieval evaluation is small and measures retrieval only (see above).
- Chunking is character-based, not section-aware; tables and equations in PDFs are extracted as plain text.
- One global indexing slot: concurrent indexing requests queue.
- Rate limits and daily quotas (all listed in `backend/.env.example`) cover AI generation, indexing, search, upload and the report/evidence reads. `/papers`, `/stats`, `/paper-file` and paper deletion are not rate-limited yet, and per-minute burst limits live in process memory, so they reset on redeploy and don't span replicas (daily quotas are stored in Postgres).

## Provenance

The frontend in `research-compass-main/` was scaffolded with Lovable and then extended by hand (API integration, auth flow, workspace, tests). The backend, schema, migrations and tests were written for this project, with AI-assisted coding (Claude Code); I designed the architecture, retrieval approach and test strategy, and reviewed all of the code. `docs/MASTER_ROADMAP.md` records the development history phase by phase.

## License

MIT — see [LICENSE](LICENSE).
