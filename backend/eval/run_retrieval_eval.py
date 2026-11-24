"""
Measure ResearchMind retrieval exactly as production runs it.

Indexes the five eval PDFs into a SEPARATE Qdrant collection
(researchmind_eval) with the production indexer's own functions, then runs
every question through the production /ask-stream retrieval path and scores
the ranked (paper, page) lists against the gold pages.

Production code reused, not re-implemented:
  pages      app.services.pdf_loader.extract_pdf_pages
  chunks     app.api.index_document._build_chunks_for_paper
             (clean_text, 100-character page skip, create_chunks, payload)
  caps       app.core.limits.max_pages_per_paper / max_chunks_per_paper
  embedding  app.rag.embedder.create_embeddings (documents), encode_query
  collection app.rag.vector_store.create_collection, its shared client
  search     ask_stream.SEARCH_LIMIT with the same owner_id filter
  rerank     app.rag.reranker.rerank_results (RERANK_ENABLED=true)
  context    ask_stream.TOP_CHUNKS and ask_stream._select_evidence

Not reproduced: keep_live_hits (a Postgres check that every hit's paper has
status 'indexed'). All five eval papers are indexed, so it would keep every
hit; it is skipped because this run makes no database call.

Stages scored, all at page level ((paper, page) of each retrieved chunk):
  vector   the 8 hits of the Qdrant query, in score order
  rerank   rerank_results' output. Production cuts to the vector top 5
           BEFORE reranking, so this stage has 5 items and recall@8 is n/a.
  context  the passages _select_evidence hands to the model

Vector store: Qdrant embedded local mode, stored in eval/cache/qdrant
(gitignored). No call goes to Qdrant Cloud: QDRANT_URL is empty for the
process, and the production modules' shared client is replaced by the local
one before anything runs. Local mode searches exactly; payload indexes are
accepted but have no effect there (create_payload_index only warns).

Safety:
  * Only VOYAGE_API_KEY is taken from the production .env, parsed in memory;
    nothing else in that file is kept, and the value is never printed or
    written anywhere.
  * Every other provider setting is overwritten with an empty value or a dead
    placeholder (Qdrant URL empty; Supabase, Postgres and Groq point at
    localhost:1), and PYTHON_DOTENV_DISABLED stops the app modules'
    load_dotenv() calls.
  * Every Qdrant call goes through _collection(), which refuses any name
    other than researchmind_eval. The production modules' COLLECTION_NAME is
    also pointed at the eval collection before anything runs.
  * Voyage calls are paced for the free tier (3 requests/min, 10K tokens/min),
    retried on 429, and cached under eval/cache/ (gitignored), so a rerun
    makes no Voyage call at all.

Usage, from backend/, with the production venv:
    python eval/run_retrieval_eval.py [--questions eval/questions_dev.jsonl]
        [--run-name baseline-dev] [--papers DIR] [--offline-check]
questions_test.jsonl is refused unless --allow-test is given.
--offline-check stops after the imports and the local collection setup,
before any Voyage call.
"""
import argparse
import hashlib
import json
import os
import statistics
import sys
import time
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from importlib.metadata import version as package_version
from pathlib import Path
from types import SimpleNamespace

EVAL_DIR = Path(__file__).resolve().parent
BACKEND_DIR = EVAL_DIR.parent
CACHE_DIR = EVAL_DIR / "cache"
RESULTS_DIR = EVAL_DIR / "results"
QDRANT_LOCAL_DIR = CACHE_DIR / "qdrant"
DEFAULT_PAPERS = BACKEND_DIR.parent.parent / "eval-papers"
SECRETS_ENV = Path(r"C:\dev\ResearchMind-AI\backend\.env")

EVAL_COLLECTION = "researchmind_eval"
PRODUCTION_COLLECTION = "researchmind_v2"
ALLOWED_KEYS = ("VOYAGE_API_KEY",)

#: Fixed identities, so reruns address the same points.
EVAL_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "researchmind/eval/retrieval")
OWNER_ID = str(uuid.uuid5(EVAL_NAMESPACE, "owner"))

#: Voyage free tier, with margin.
MIN_REQUEST_SPACING_S = 21.0
TOKENS_PER_MINUTE_BUDGET = 9000
MAX_429_RETRIES = 8
BACKOFF_429_S = 30.0

KS = (1, 3, 5, 8)


# ----------------------------------------------------------- environment

def _read_allowed_keys(path):
    """The allowed values (ALLOWED_KEYS) from the production .env, nothing else."""
    found = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in ALLOWED_KEYS:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        found[key] = value
    missing = [k for k in ALLOWED_KEYS if not found.get(k)]
    if missing:
        raise SystemExit(f"Missing in the production .env: {', '.join(missing)}")
    return found


def configure_environment():
    """Must run before any app module is imported."""
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    dead = "http://localhost:1"
    os.environ.update({
        # Never used by this run; dead values so an accidental call fails.
        "GROQ_API_KEY": "eval-placeholder-not-a-key",
        "SUPABASE_URL": dead,
        "SUPABASE_ANON_KEY": "eval-placeholder",
        "SUPABASE_SERVICE_ROLE_KEY": "eval-placeholder",
        "DATABASE_URL": "postgresql://eval:eval@localhost:1/none",
        # No Qdrant Cloud: the shared client is replaced by an embedded one.
        "QDRANT_URL": "",
        "QDRANT_API_KEY": "",
        # The rerank stage is measured with reranking on.
        "RERANK_ENABLED": "true",
        # Pacing is done by PacedCachingVoyage below, which also covers the
        # query and rerank calls; the embedder's own between-batch sleep
        # would only double it. Batch composition is unchanged.
        "VOYAGE_EMBED_PACING_SECONDS": "0",
    })
    os.environ.update(_read_allowed_keys(SECRETS_ENV))
    sys.path.insert(0, str(BACKEND_DIR))


def _collection(name):
    """Every Qdrant call names its collection through this check."""
    if name != EVAL_COLLECTION or name == PRODUCTION_COLLECTION:
        raise SystemExit(f"Refusing a Qdrant call on collection {name!r}")
    return name


# ------------------------------------------------------- Voyage cache layer

def _digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode("utf-8")).hexdigest()


class PacedCachingVoyage:
    """Stands in for the voyageai.Client that app.rag.embedder and
    app.rag.reranker create lazily: it is installed as their module-level
    _client, so the production functions run unchanged and only the network
    call is cached and paced. Latency is the duration of the successful API
    call alone, without pacing or backoff waits; a cached result reports the
    latency measured when it was first fetched."""

    def __init__(self, real_client, cache_dir, estimate_tokens):
        self.real = real_client
        self.cache_dir = cache_dir
        self.estimate_tokens = estimate_tokens
        self.last_request_at = 0.0
        self.window = []  # (time, tokens)
        self.stats = Counter()
        self.wait_seconds = 0.0
        self.last = None  # metadata of the most recent call

    # -- pacing
    def _wait_for_budget(self, estimated_tokens):
        while True:
            now = time.monotonic()
            self.window = [(t, n) for t, n in self.window if now - t < 60]
            used = sum(n for _, n in self.window)
            wait = max(0.0, self.last_request_at + MIN_REQUEST_SPACING_S - now)
            if used + estimated_tokens > TOKENS_PER_MINUTE_BUDGET and self.window:
                wait = max(wait, self.window[0][0] + 60 - now + 0.5)
            if wait <= 0:
                return
            self.wait_seconds += wait
            time.sleep(wait)

    def _call(self, fn, estimated_tokens):
        from voyageai.error import RateLimitError

        for attempt in range(MAX_429_RETRIES + 1):
            self._wait_for_budget(estimated_tokens)
            self.last_request_at = time.monotonic()
            started = time.perf_counter()
            try:
                result = fn()
            except RateLimitError:
                self.stats["429"] += 1
                if attempt == MAX_429_RETRIES:
                    raise
                pause = BACKOFF_429_S * (attempt + 1)
                self.wait_seconds += pause
                time.sleep(pause)
                continue
            latency = time.perf_counter() - started
            tokens = getattr(result, "total_tokens", None) or estimated_tokens
            self.window.append((time.monotonic(), tokens))
            return result, latency, tokens
        raise RuntimeError("unreachable")

    def _cached(self, kind, key_obj, fetch, estimated_tokens):
        path = self.cache_dir / kind / f"{_digest(key_obj)}.json"
        if path.exists():
            self.stats[f"{kind}_cached"] += 1
            record = json.loads(path.read_text(encoding="utf-8"))
            self.last = {"cached": True, "latency_s": record["latency_s"]}
            return record
        result, latency, tokens = self._call(fetch, estimated_tokens)
        self.stats[f"{kind}_api"] += 1
        self.stats[f"{kind}_tokens"] += tokens
        record = {"latency_s": latency, "total_tokens": tokens, **self._serialise(kind, result)}
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record), encoding="utf-8")
        tmp.replace(path)
        self.last = {"cached": False, "latency_s": latency}
        return record

    @staticmethod
    def _serialise(kind, result):
        if kind == "embed":
            return {"embeddings": result.embeddings}
        # Only indices and scores: no document text in the cache.
        return {"results": [{"index": r.index, "relevance_score": r.relevance_score}
                            for r in result.results]}

    # -- the two methods production calls
    def embed(self, texts, model, input_type, **kwargs):
        record = self._cached(
            "embed",
            {"model": model, "input_type": input_type, "texts": list(texts), "kw": kwargs},
            lambda: self.real.embed(texts, model=model, input_type=input_type, **kwargs),
            sum(self.estimate_tokens(t) for t in texts),
        )
        return SimpleNamespace(embeddings=record["embeddings"])

    def rerank(self, query, documents, model, **kwargs):
        record = self._cached(
            "rerank",
            {"model": model, "query": query, "documents": list(documents), "kw": kwargs},
            lambda: self.real.rerank(query=query, documents=documents, model=model, **kwargs),
            sum(self.estimate_tokens(query + d) for d in documents),
        )
        self.last["scores"] = [r["relevance_score"] for r in record["results"]]
        return SimpleNamespace(results=[
            SimpleNamespace(index=r["index"], relevance_score=r["relevance_score"],
                            document=documents[r["index"]])
            for r in record["results"]
        ])


# ---------------------------------------------------------------- indexing

def paper_uuid(file_name):
    return str(uuid.uuid5(EVAL_NAMESPACE, f"paper/{file_name}"))


def index_papers(app, pdfs, voyage, log):
    """Index each PDF as production does, skipping papers already present."""
    client = app.vector_store.client
    manifest_path = CACHE_DIR / "index_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}

    _collection(app.vector_store.COLLECTION_NAME)
    app.vector_store.create_collection()  # production function, eval name (patched)
    summary = {}
    for pdf in pdfs:
        pages = app.extract_pdf_pages(str(pdf))
        if len(pages) > app.limits.max_pages_per_paper():
            raise SystemExit(f"{pdf.name}: {len(pages)} pages exceeds the production cap")
        paper = SimpleNamespace(id=paper_uuid(pdf.name), owner_id=OWNER_ID, title=pdf.name)
        chunks = app.build_chunks(paper, pages)
        if len(chunks) > app.limits.max_chunks_per_paper():
            raise SystemExit(f"{pdf.name}: {len(chunks)} chunks exceeds the production cap")

        fingerprint = _digest({"model": app.embedder.VOYAGE_EMBED_MODEL, "chunks": chunks})
        paper_filter = app.Filter(must=[
            app.FieldCondition(key="paper_id", match=app.MatchValue(value=paper.id)),
            app.FieldCondition(key="owner_id", match=app.MatchValue(value=OWNER_ID)),
        ])
        present = client.count(collection_name=_collection(EVAL_COLLECTION),
                               count_filter=paper_filter, exact=True).count
        info = {"pages": len(pages), "chunks": len(chunks),
                "indexed_pages": len({c["page"] for c in chunks})}
        if manifest.get(pdf.name) == fingerprint and present == len(chunks):
            log(f"  {pdf.name}: {len(chunks)} chunks already indexed, skipped")
            summary[pdf.name] = {**info, "action": "skipped"}
            continue

        log(f"  {pdf.name}: {len(pages)} pages, {len(chunks)} chunks, embedding...")
        embeddings = app.embedder.create_embeddings([c["text"] for c in chunks])
        points = [
            # Production uses uuid4; deterministic ids only make reruns
            # idempotent and do not affect retrieval.
            app.PointStruct(id=str(uuid.uuid5(EVAL_NAMESPACE, f"{paper.id}/{i}")),
                            vector=vec, payload=item)
            for i, (item, vec) in enumerate(zip(chunks, embeddings))
        ]
        client.delete(collection_name=_collection(EVAL_COLLECTION), points_selector=paper_filter)
        client.upsert(collection_name=_collection(EVAL_COLLECTION), points=points)
        manifest[pdf.name] = fingerprint
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
        summary[pdf.name] = {**info, "action": "indexed"}
    return summary


# --------------------------------------------------------------- retrieval

def retrieve(app, voyage, question):
    """The /ask-stream retrieval path for a first-turn question."""
    client = app.vector_store.client
    out = {"library_question": app.is_library_question(question)}

    query_vector = app.embedder.encode_query(question)
    embed_meta = dict(voyage.last)

    owner_filter = app.Filter(must=[
        app.FieldCondition(key="owner_id", match=app.MatchValue(value=OWNER_ID))])
    started = time.perf_counter()
    hits = client.query_points(
        collection_name=_collection(EVAL_COLLECTION),
        query=query_vector,
        query_filter=owner_filter,
        limit=app.ask_stream.SEARCH_LIMIT,
    ).points
    search_s = time.perf_counter() - started
    # keep_live_hits(owner_id, hits) would keep all: every eval paper is indexed.

    voyage.last = None
    ranked = app.reranker.rerank_results(question, hits)
    rerank_meta = dict(voyage.last) if voyage.last else {"cached": None, "latency_s": None, "scores": []}

    filtered = ranked[:app.ask_stream.TOP_CHUNKS]
    selected = app.ask_stream._select_evidence(filtered)

    out.update(hits=hits, ranked=ranked, rerank_scores=rerank_meta.get("scores", []),
               selected=[hit for hit, _ in selected])
    out["latency_ms"] = {
        "query_embed": round(embed_meta["latency_s"] * 1000, 1),
        "query_embed_cached": embed_meta["cached"],
        "vector_search": round(search_s * 1000, 1),
        "rerank": round(rerank_meta["latency_s"] * 1000, 1) if rerank_meta["latency_s"] else None,
        "rerank_cached": rerank_meta["cached"],
    }
    return out


# ----------------------------------------------------------------- scoring

def gold_of(q):
    pages = {(g["paper"], p) for g in q["gold"] for p in g["pages"]}
    papers = {g["paper"] for g in q["gold"]}
    return pages, papers


def is_hit(q, entry):
    pages, papers = gold_of(q)
    if q.get("gold_level") == "paper":
        return entry["paper"] in papers
    return (entry["paper"], entry["page"]) in pages


def stage_metrics(q, entries, has_k8=True):
    hits = [is_hit(q, e) for e in entries]
    first = next((i for i, h in enumerate(hits) if h), None)
    _, papers = gold_of(q)
    m = {}
    for k in KS:
        m[f"recall@{k}"] = (any(hits[:k]) if (k <= 5 or has_k8) else None)
    m["mrr@5"] = 1 / (first + 1) if first is not None and first < 5 else 0.0
    m["mrr"] = 1 / (first + 1) if first is not None else 0.0
    m["paper_hit@5"] = any(e["paper"] in papers for e in entries[:5])
    m["first_hit_rank"] = first + 1 if first is not None else None
    if len(papers) > 1:
        found = {e["paper"] for e in entries[:8]} & papers
        m["gold_paper_share"] = len(found) / len(papers)
    return m


def describe(hit, page_kinds, rank, **extra):
    p = hit.payload
    entry = {"rank": rank, "paper": p.get("paper"), "page": p.get("page"),
             "chunk_id": p.get("chunk_id"),
             "page_class": page_kinds.get((p.get("paper"), p.get("page")), "unknown")}
    entry.update(extra)
    return entry


def mean(values):
    values = [float(v) for v in values if v is not None]
    return sum(values) / len(values) if values else None


def aggregate(items, stage):
    rows = [it["metrics"][stage] for it in items]
    agg = {"n": len(rows)}
    for key in [f"recall@{k}" for k in KS] + ["mrr@5", "mrr", "paper_hit@5"]:
        agg[key] = mean(r[key] for r in rows)
    shares = [r["gold_paper_share"] for r in rows if "gold_paper_share" in r]
    if shares:
        agg["gold_paper_share"] = mean(shares)
    return agg


def quantiles(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    def q(p):
        return values[min(len(values) - 1, int(round(p * (len(values) - 1))))]
    return {"n": len(values), "min": values[0], "p25": q(0.25), "median": statistics.median(values),
            "p75": q(0.75), "max": values[-1]}


# ---------------------------------------------------------------- report

def fmt(v, pct=True):
    if v is None:
        return "n/a"
    return f"{v * 100:.0f}%" if pct else f"{v:.3f}"


def markdown_summary(run):
    m = run["metrics"]
    lines = [
        f"# Retrieval eval: {run['run_name']}",
        "",
        f"Run {run['started_utc']} on `{run['questions_file']}` "
        f"({run['counts']['questions']} questions: {run['counts']['answerable']} answerable, "
        f"{run['counts']['unanswerable']} unanswerable). Collection `{EVAL_COLLECTION}`. "
        f"Wall time {run['timing']['total_s']:.0f} s, of which {run['timing']['voyage_wait_s']:.0f} s "
        "was free-tier pacing.",
        "",
        "## Production settings measured",
        "",
    ]
    lines += [f"- {s}" for s in run["settings"]["summary"]]
    lines += [
        "",
        "## Metrics (answerable items, page level)",
        "",
        "Hit = a retrieved chunk is on any gold page of any gold paper (`cross-09`: any page of a "
        "gold paper). k counts chunks, as production does. The rerank stage has only 5 items "
        "(production cuts to the vector top 5 before reranking), so its recall@8 is n/a and its "
        "recall@5 equals the vector stage's. `context` = passages handed to the model.",
        "",
        "| Stage | Type | n | R@1 | R@3 | R@5 | R@8 | MRR@5 | MRR | paper-hit@5 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for stage in ("vector", "rerank"):
        for group, agg in m[stage].items():
            lines.append(
                f"| {stage} | {group} | {agg['n']} | {fmt(agg['recall@1'])} | {fmt(agg['recall@3'])} | "
                f"{fmt(agg['recall@5'])} | {fmt(agg['recall@8'])} | {fmt(agg['mrr@5'], False)} | "
                f"{fmt(agg['mrr'], False)} | {fmt(agg['paper_hit@5'])} |")
    ctx = m["context"]
    lines += [
        "",
        f"Context stage: a gold page reaches the model for {fmt(ctx['all']['hit'])} of answerable items "
        f"(mean {ctx['all']['passages']:.1f} passages).",
        "",
        "## Cross-paper items",
        "",
        "| id | gold papers | found in vector top 8 | found in rerank list | found in context |",
        "|---|---|---|---|---|",
    ]
    for row in run["cross_paper"]:
        lines.append(f"| {row['id']} | {row['gold_papers']} | {fmt(row['vector_share'])} | "
                     f"{fmt(row['rerank_share'])} | {fmt(row['context_share'])} |")
    u = run["unanswerable"]
    lines += [
        "",
        "## Unanswerable items: top rerank score",
        "",
        "| id | top rerank score | top vector score | top page (class) |",
        "|---|---|---|---|",
    ]
    for row in u["items"]:
        lines.append(f"| {row['id']} | {row['top_rerank_score']:.3f} | {row['top_vector_score']:.3f} | "
                     f"{row['top_page']} ({row['top_page_class']}) |")
    a = u["answerable_top_rerank"]
    lines += [
        "",
        f"Answerable items' top rerank score: min {a['min']:.3f}, p25 {a['p25']:.3f}, "
        f"median {a['median']:.3f}, p75 {a['p75']:.3f}, max {a['max']:.3f} (n={a['n']}). "
        f"Answerable items whose top rerank score is below the highest unanswerable top score: "
        f"{u['answerable_below_max_unanswerable']} of {a['n']}.",
        "",
        "## Retrieved page classes (share of retrieved chunks, all questions)",
        "",
        "| Stage | body | mixed | reference |",
        "|---|---|---|---|",
    ]
    for stage, shares in run["page_class_share"].items():
        lines.append(f"| {stage} | {fmt(shares.get('body', 0))} | {fmt(shares.get('mixed', 0))} | "
                     f"{fmt(shares.get('reference', 0))} |")
    lines += [
        "",
        "## Worst misses",
        "",
        "Order: no gold page in the vector top 8 first, then later first-hit rank after rerank, "
        "then higher top rerank score (confidently wrong).",
        "",
    ]
    for w in run["worst_misses"]:
        lines.append(f"- **{w['id']}** ({w['type']}): {w['question']}  ")
        lines.append(f"  gold {w['gold']}; first gold hit: vector rank {w['vector_first_hit']}, "
                     f"rerank rank {w['rerank_first_hit']}  ")
        lines.append("  rerank list: " + ", ".join(
            f"{e['paper']} p{e['page']} ({e['page_class']})" for e in w["rerank"]))
    lat = run["latency_ms"]
    lines += [
        "",
        "## Latency per stage (ms, from this machine; not production's network)",
        "",
        "| Stage | median | p95 | max |",
        "|---|---|---|---|",
    ]
    for stage, s in lat.items():
        if s:
            lines.append(f"| {stage} | {s['median']:.0f} | {s['p95']:.0f} | {s['max']:.0f} |")
    lines += ["", "Voyage and rerank latencies are the first (uncached) call's, stored in the cache.",
              "", f"Voyage calls this run: {run['voyage_calls']}", ""]
    return "\n".join(lines)


# ------------------------------------------------------------------- main

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--questions", default=str(EVAL_DIR / "questions_dev.jsonl"))
    parser.add_argument("--run-name", default="baseline-dev")
    parser.add_argument("--papers", default=str(DEFAULT_PAPERS))
    parser.add_argument("--allow-test", action="store_true",
                        help="permit questions_test.jsonl (final comparison only)")
    parser.add_argument("--offline-check", action="store_true",
                        help="stop after imports and local collection setup, before any Voyage call")
    args = parser.parse_args(argv)

    if Path(args.questions).name == "questions_test.jsonl" and not args.allow_test:
        raise SystemExit("questions_test.jsonl is held out; pass --allow-test for the final run")

    started_wall = time.perf_counter()
    started_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    log = lambda msg: print(msg, flush=True)  # noqa: E731

    configure_environment()

    import voyageai
    from qdrant_client import QdrantClient
    from qdrant_client.local.qdrant_local import QdrantLocal
    from qdrant_client.models import FieldCondition, Filter, MatchValue, PointStruct

    from app.api import ask_stream, index_document
    from app.api.index_document import _build_chunks_for_paper
    from app.core import limits
    from app.core.providers import voyage_client_options
    from app.rag import embedder, reranker, vector_store
    from app.services.library_questions import is_library_question
    from app.services.pdf_loader import extract_pdf_pages
    import check_questions

    # Replace the shared Qdrant client with an embedded one, in every loaded
    # app module that imported it by name (vector_store, index_document,
    # ask_stream, ...), before any Qdrant call is made. The client built at
    # import time saw an empty QDRANT_URL, so it was never a Cloud client and
    # it is never used.
    QDRANT_LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    local_client = QdrantClient(path=str(QDRANT_LOCAL_DIR))
    import_time_client = vector_store.client
    for name, module in list(sys.modules.items()):
        if (name == "app" or name.startswith("app.")) and getattr(module, "client", None) is import_time_client:
            module.client = local_client
    for module in (vector_store, index_document, ask_stream):
        assert module.client is local_client
    assert isinstance(local_client._client, QdrantLocal)

    # Point every production module that names the collection at the eval one.
    for module in (vector_store, index_document, ask_stream):
        assert module.COLLECTION_NAME == PRODUCTION_COLLECTION
        module.COLLECTION_NAME = _collection(EVAL_COLLECTION)

    if args.offline_check:
        _collection(vector_store.COLLECTION_NAME)
        vector_store.create_collection()  # production function, eval name, local store
        names = [c.name for c in local_client.get_collections().collections]
        info = local_client.get_collection(_collection(EVAL_COLLECTION))
        log(f"Offline check: local collections {names}; {EVAL_COLLECTION} has "
            f"{info.points_count} points, vectors {info.config.params.vectors.size}-d "
            f"{info.config.params.vectors.distance}. No Voyage call made.")
        local_client.close()
        return

    voyage = PacedCachingVoyage(
        voyageai.Client(api_key=os.environ["VOYAGE_API_KEY"], **voyage_client_options()),
        CACHE_DIR, embedder.estimate_tokens)
    embedder._client = voyage
    reranker._client = voyage
    assert reranker._rerank_enabled()

    app = SimpleNamespace(
        ask_stream=ask_stream, build_chunks=_build_chunks_for_paper, limits=limits,
        embedder=embedder, reranker=reranker, vector_store=vector_store,
        is_library_question=is_library_question, extract_pdf_pages=extract_pdf_pages,
        Filter=Filter, FieldCondition=FieldCondition, MatchValue=MatchValue,
        PointStruct=PointStruct)

    pdfs = sorted(Path(args.papers).glob("*.pdf"))
    papers = check_questions.load_papers(args.papers)
    page_kinds = {(name, p.number): p.kind for name, pages in papers.items() for p in pages}

    log(f"Indexing {len(pdfs)} papers into {EVAL_COLLECTION}")
    t0 = time.perf_counter()
    index_summary = index_papers(app, pdfs, voyage, log)
    index_s = time.perf_counter() - t0

    questions = [json.loads(line) for line in
                 Path(args.questions).read_text(encoding="utf-8").splitlines() if line.strip()]
    log(f"Running {len(questions)} questions")
    t0 = time.perf_counter()
    items = []
    for n, q in enumerate(questions, 1):
        r = retrieve(app, voyage, q["question"])
        vec = [describe(h, page_kinds, i + 1, score=round(h.score, 4)) for i, h in enumerate(r["hits"])]
        vector_rank = {h.id: i + 1 for i, h in enumerate(r["hits"])}
        rer = [describe(h, page_kinds, i + 1, vector_rank=vector_rank[h.id],
                        rerank_score=(round(r["rerank_scores"][i], 4) if i < len(r["rerank_scores"]) else None))
               for i, h in enumerate(r["ranked"])]
        ctx = [describe(h, page_kinds, i + 1, vector_rank=vector_rank[h.id]) for i, h in enumerate(r["selected"])]
        item = {
            "id": q["id"], "type": q["type"], "question": q["question"], "gold": q["gold"],
            "gold_level": q.get("gold_level", "page"), "library_question": r["library_question"],
            "vector": vec, "rerank": rer, "context": ctx, "latency_ms": r["latency_ms"],
        }
        if q["type"] != "unanswerable":
            item["metrics"] = {
                "vector": stage_metrics(q, vec),
                "rerank": stage_metrics(q, rer, has_k8=False),
                "context": {"hit": any(is_hit(q, e) for e in ctx), "passages": len(ctx)},
            }
            if len(gold_of(q)[1]) > 1:
                item["metrics"]["context"]["gold_paper_share"] = (
                    len({e["paper"] for e in ctx} & gold_of(q)[1]) / len(gold_of(q)[1]))
        items.append(item)
        log(f"  [{n}/{len(questions)}] {q['id']}")
    query_s = time.perf_counter() - t0

    answerable = [it for it in items if it["type"] != "unanswerable"]
    unanswerable = [it for it in items if it["type"] == "unanswerable"]
    by_type = defaultdict(list)
    for it in answerable:
        by_type[it["type"]].append(it)

    metrics = {}
    for stage in ("vector", "rerank"):
        metrics[stage] = {"all": aggregate(answerable, stage)}
        for t in sorted(by_type):
            metrics[stage][t] = aggregate(by_type[t], stage)
    metrics["context"] = {"all": {"hit": mean(it["metrics"]["context"]["hit"] for it in answerable),
                                  "passages": mean(it["metrics"]["context"]["passages"] for it in answerable)}}
    for t in sorted(by_type):
        metrics["context"][t] = {"hit": mean(it["metrics"]["context"]["hit"] for it in by_type[t]),
                                 "passages": mean(it["metrics"]["context"]["passages"] for it in by_type[t])}

    cross = [{
        "id": it["id"],
        "gold_papers": len(gold_of(next(q for q in questions if q["id"] == it["id"]))[1]),
        "vector_share": it["metrics"]["vector"].get("gold_paper_share"),
        "rerank_share": it["metrics"]["rerank"].get("gold_paper_share"),
        "context_share": it["metrics"]["context"].get("gold_paper_share"),
    } for it in by_type.get("cross-paper", [])]

    answerable_top = [it["rerank"][0]["rerank_score"] for it in answerable if it["rerank"]]
    unans_rows = [{
        "id": it["id"],
        "top_rerank_score": it["rerank"][0]["rerank_score"],
        "top_vector_score": it["vector"][0]["score"],
        "top_page": f"{it['rerank'][0]['paper']} p{it['rerank'][0]['page']}",
        "top_page_class": it["rerank"][0]["page_class"],
    } for it in unanswerable]
    max_unans = max((r["top_rerank_score"] for r in unans_rows), default=None)
    unans = {
        "items": unans_rows,
        "answerable_top_rerank": quantiles(answerable_top),
        "answerable_top_vector": quantiles([it["vector"][0]["score"] for it in answerable]),
        "answerable_below_max_unanswerable":
            sum(1 for s in answerable_top if max_unans is not None and s <= max_unans),
    }

    class_share = {}
    for stage in ("vector", "rerank", "context"):
        counts = Counter(e["page_class"] for it in items for e in it[stage])
        total = sum(counts.values())
        class_share[stage] = {k: v / total for k, v in counts.items()} if total else {}

    def severity(it):
        v, r = it["metrics"]["vector"], it["metrics"]["rerank"]
        return (v["recall@8"], r["first_hit_rank"] is not None,
                -(r["first_hit_rank"] or 99), -(it["rerank"][0]["rerank_score"] or 0))
    misses = [it for it in sorted(answerable, key=severity) if not it["metrics"]["rerank"]["recall@1"]][:5]
    worst = [{
        "id": it["id"], "type": it["type"], "question": it["question"],
        "gold": "; ".join(f"{g['paper']} p{','.join(map(str, g['pages']))}" for g in it["gold"]),
        "vector_first_hit": it["metrics"]["vector"]["first_hit_rank"],
        "rerank_first_hit": it["metrics"]["rerank"]["first_hit_rank"],
        "rerank": it["rerank"], "vector": it["vector"],
    } for it in misses]

    def lat(key):
        vals = sorted(it["latency_ms"][key] for it in items if it["latency_ms"][key] is not None)
        if not vals:
            return None
        return {"median": statistics.median(vals),
                "p95": vals[min(len(vals) - 1, int(round(0.95 * (len(vals) - 1))))], "max": vals[-1]}
    latency = {"query_embed": lat("query_embed"), "vector_search": lat("vector_search"),
               "rerank": lat("rerank")}

    total_s = time.perf_counter() - started_wall
    run = {
        "run_name": args.run_name,
        "started_utc": started_utc,
        "questions_file": f"eval/{Path(args.questions).name}",
        "collection": EVAL_COLLECTION,
        "owner_id": OWNER_ID,
        "settings": {
            "vector_store": "qdrant embedded local mode (exact search), eval/cache/qdrant",
            "embed_model": embedder.VOYAGE_EMBED_MODEL,
            "rerank_model": reranker.VOYAGE_RERANK_MODEL,
            "search_limit": ask_stream.SEARCH_LIMIT,
            "max_rerank": reranker.MAX_RERANK,
            "rerank_doc_chars": 400,
            "top_chunks": ask_stream.TOP_CHUNKS,
            "max_context_chars": ask_stream.MAX_CONTEXT,
            "rerank_enabled": True,
            "keep_live_hits": "not called (needs Postgres); all eval papers are indexed, so it keeps every hit",
            "summary": [
                "Pages: PyMuPDF physical pages from 1, clean_text per page, pages under 100 chars "
                "skipped; RecursiveCharacterTextSplitter 1500 chars, 300 overlap; payload paper = file name.",
                f"Embeddings: Voyage {embedder.VOYAGE_EMBED_MODEL}, input_type document for chunks and "
                "query for the question; Qdrant 1024-d cosine.",
                f"Search: query_points limit {ask_stream.SEARCH_LIMIT}, owner_id filter; keep_live_hits "
                "skipped (would keep every hit here).",
                f"Rerank (RERANK_ENABLED=true): vector top {reranker.MAX_RERANK} only, Voyage "
                f"{reranker.VOYAGE_RERANK_MODEL} on each chunk's first 400 chars. With the default "
                "RERANK_ENABLED=false production keeps the vector top 5 unchanged.",
                "Vector search: Qdrant embedded local mode (exact search). At this collection "
                "size Qdrant Cloud generally searches exactly too, so rankings should match; "
                "not verified against Cloud.",
                f"Context: top {ask_stream.TOP_CHUNKS} after rerank, _select_evidence within "
                f"{ask_stream.MAX_CONTEXT} chars.",
            ],
        },
        "index": index_summary,
        "counts": {"questions": len(items), "answerable": len(answerable),
                   "unanswerable": len(unanswerable),
                   "library_routed": sum(it["library_question"] for it in items)},
        "metrics": metrics,
        "cross_paper": cross,
        "unanswerable": unans,
        "page_class_share": class_share,
        "worst_misses": worst,
        "latency_ms": latency,
        "timing": {"total_s": round(total_s, 1), "index_s": round(index_s, 1),
                   "questions_s": round(query_s, 1), "voyage_wait_s": round(voyage.wait_seconds, 1)},
        "voyage_calls": dict(voyage.stats),
        "versions": {"python": sys.version.split()[0],
                     **{name: package_version(name) for name in ("voyageai", "qdrant-client", "pymupdf")}},
        "items": items,
    }
    RESULTS_DIR.mkdir(exist_ok=True)
    (RESULTS_DIR / f"{args.run_name}.json").write_text(json.dumps(run, indent=1), encoding="utf-8")
    (RESULTS_DIR / f"{args.run_name}.md").write_text(markdown_summary(run), encoding="utf-8")
    local_client.close()
    log(f"Done in {total_s:.0f} s. Results: eval/results/{args.run_name}.json and .md")


if __name__ == "__main__":
    main()
