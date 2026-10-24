# ResearchMind AI

A private research workspace for academic PDFs. You upload papers, they are indexed into a vector database, and you can search them, ask questions with answers grounded in cited passages, compare papers, and export reports. Every query is scoped to the signed-in user's own library.

> **Live app:** `[ADD URL]` · **Demo video (2 min):** `[ADD LINK]`
> Status: personal project, built and maintained by Harish Kumar S G (solo).

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
  └── Groq               answer generation
```

**Design decisions worth knowing**

- **Tenant isolation is enforced twice:** an explicit `owner_id` filter in every query, and Postgres row-level security driven by the same verified identity. `owner_id` is never accepted from the client.
- **Retrieval:** recursive character chunking (1,500 chars, 300 overlap), top-8 vector search, optional rerank to top-5.
- **Grounding:** answers are generated only from retrieved passages; quotes in structured outputs are validated against the source text before being shown.
- **Cost control:** quotas and per-paper caps are checked *before* any embedding or generation call.

## Tests

The backend suite runs fully offline: providers are mocked and persistence uses in-memory SQLite.

```bash
cd backend
pip install -r requirements.txt
# Placeholder values only; no real keys are needed for the offline suite.
export GROQ_API_KEY=x VOYAGE_API_KEY=x QDRANT_URL=http://localhost:1 QDRANT_API_KEY=x \
       SUPABASE_URL=http://localhost:1 SUPABASE_ANON_KEY=x SUPABASE_SERVICE_ROLE_KEY=x
python -m unittest discover -s tests
```

Tests that need live infrastructure are skipped unless `RUN_LIVE_INFRA_TESTS=1`. Frontend: `cd research-compass-main && npm ci && npm test`. Both run in CI (`.github/workflows/tests.yml`).

## Retrieval evaluation

`[PENDING — not yet measured.]` Planned: a labelled question set over a fixed paper set, reporting retrieval recall@k and MRR with and without reranking, citation correctness, and answer latency. Results will be added here only after they are run.

## Running locally

1. Create a Supabase project, enable Google OAuth, and a Qdrant Cloud cluster.
2. Apply `backend/migrations/bootstrap/app_role.sql`, then `0001` to `0007` in order. Set the app role's password out of band.
3. Copy `backend/.env.example` to `backend/.env` and `research-compass-main/.env.example` to `research-compass-main/.env`, and fill in your own values.
4. Backend: `cd backend && uvicorn app.main:app --reload` (API docs at `/docs`).
5. Frontend: `cd research-compass-main && npm ci && npm run dev`.

## Known limitations

- No retrieval-quality benchmark yet (see above).
- Chunking is character-based, not section-aware; tables and equations in PDFs are extracted as plain text.
- One global indexing slot: concurrent indexing requests queue.
- Some limits in `.env.example` are configured but not yet enforced on read endpoints.

## Provenance

The frontend in `research-compass-main/` was scaffolded with Lovable and then extended by hand (API integration, auth flow, workspace, tests). The backend, schema, migrations and tests were written for this project. `docs/MASTER_ROADMAP.md` records the development history phase by phase.

## License

`[CHOOSE: MIT / Apache-2.0 — add a LICENSE file]`
