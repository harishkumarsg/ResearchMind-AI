# CLAUDE.md — ResearchMind AI

Research-paper assistant: upload PDFs, index them, ask questions answered with cited passages.

## Architecture
- **Backend** `backend/`: FastAPI (Python 3.11), deployed on **Railway** (`backend/railway.toml`, Nixpacks, healthcheck `/health`).
- **Frontend** `research-compass-main/`: React + TanStack Start (Vite, nitro), deployed on **Vercel**. Scaffolded with Lovable.
- **Supabase**: Postgres (RLS forced, app role `researchmind_app`), Auth (JWT verified via JWKS), Storage (PDFs).
- **Qdrant Cloud**: vectors (collection `researchmind_v2`). **Voyage**: embeddings and optional rerank. **Groq**: generation.

## Layout
```
backend/app/api/        one router per endpoint (upload, index-document, ask-stream, research, ...)
backend/app/core/       auth, CORS, rate limits, quotas, provider timeouts
backend/app/db/         SQLAlchemy models + session (binds JWT claims for RLS)
backend/app/rag/        chunker, embedder, reranker, vector_store
backend/app/services/   storage, pdf_loader, intelligence/relationship stores
backend/migrations/     0001..0007 SQL + bootstrap/app_role.sql (apply in order)
backend/tests/          unittest suite, offline by default
research-compass-main/src/{routes,components,lib}   UI, API client (lib/api.ts), auth context
docs/MASTER_ROADMAP.md  development history and backlog
```

## Environment variables (names only; values live in .env / platform dashboards)
- Backend required: `GROQ_API_KEY VOYAGE_API_KEY QDRANT_URL QDRANT_API_KEY SUPABASE_URL SUPABASE_ANON_KEY SUPABASE_SERVICE_ROLE_KEY DATABASE_URL`
- Backend optional: `CORS_EXTRA_ORIGINS RERANK_ENABLED QUOTA_ENFORCEMENT`, `*_RATE_PER_MINUTE`, `*_QUOTA_PER_DAY`, `GROQ_*`/`VOYAGE_*` timeouts and retries, `MAX_PAGES_PER_PAPER MAX_CHUNKS_PER_PAPER INDEX_SLOT_WAIT_SECONDS`
- Frontend: `VITE_API_BASE_URL VITE_SUPABASE_URL VITE_SUPABASE_ANON_KEY`
- **Supabase keys are the new kind; legacy JWT keys are disabled on this project.** `SUPABASE_ANON_KEY` holds the publishable key (`sb_publishable_…`) and `SUPABASE_SERVICE_ROLE_KEY` a secret key (`sb_secret_…`). The names are historical and kept so deployments need no rename. A legacy `eyJ…` key is rejected, which broke every upload and `/paper-file` on 2026-11-23.

## Run and test (keep output quiet)
Backend (Python 3.11; the offline suite needs only placeholder values):
```bash
cd backend
pip install --require-hashes --only-binary=:all: -r requirements.txt
export GROQ_API_KEY=x VOYAGE_API_KEY=x QDRANT_URL=http://localhost:1 QDRANT_API_KEY=x \
       SUPABASE_URL=http://localhost:1 SUPABASE_ANON_KEY=x SUPABASE_SERVICE_ROLE_KEY=x
python -m unittest discover -s tests -q      # report totals and failures only
uvicorn app.main:app --reload                # dev server, docs at /docs
```
Live-infra tests are skipped unless `RUN_LIVE_INFRA_TESTS=1`. Never run them against production.
`app/` and several test modules call `load_dotenv()`, which finds `backend/.env` from inside `backend/`. To run the suite without the real `.env`, run it from a checkout that has none (e.g. a `git worktree` outside the repo).

Frontend:
```bash
cd research-compass-main
npm ci --legacy-peer-deps     # flag is a temporary peer-range workaround
npx vitest run --reporter=dot
npm run dev
```
CI: `.github/workflows/tests.yml` runs both suites on pushes to master and on PRs.

## Deployment
- A push to `master` deploys **production** (Railway backend, Vercel frontend).
- Database migrations are applied by hand in Supabase, in order, never automatically.
- `render.yaml` was deleted; Render is no longer used.

## Rules
1. **Never push to `master`** or merge to it without the owner's explicit OK. Work on branches; open PRs only when asked.
2. **Never print, log or commit secrets.** Do not open or echo `backend/.env` or `research-compass-main/.env`. Scan diffs for keys before committing.
3. **Don't invent numbers.** Test counts, metrics and benchmarks must come from an actual run; otherwise write "not measured".
4. **Credit AI-assisted work honestly** in commit messages.
5. Private, never committed (gitignored): `docs/blueprint/`, `README.local.md`, `STATUS_REPORT.md`.
6. Tests must stay offline and hermetic; mock providers and never touch the live Supabase or Qdrant from the suite.
7. Don't weaken security invariants: owner ID comes only from the verified JWT; RLS stays forced; keep error messages sanitized.
8. Ask before destructive or hard-to-reverse actions: deleting data, re-indexing, migrations, force-pushes.
