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

Variants (--variant, default baseline; see VARIANTS) measure candidate
fixes with the same scoring: rerank-off, no-refs, no-refs-top20-rerank10 and
no-refs-top20-rerank10-ctx6000. Production code is not changed: a variant
only filters the pages handed to the production chunker, changes the search
limit or MAX_CONTEXT for this process, sets RERANK_ENABLED, or (rerank10)
replaces the rerank call with rerank_full_text. Each run writes
results/<variant>-<split>.json and .md and rewrites results/compare-<split>.md.

Usage, from backend/, with the production venv:
    python eval/run_retrieval_eval.py [--variant baseline]
        [--questions eval/questions_dev.jsonl] [--run-name NAME] [--papers DIR]
        [--offline-check] [--cache-only] [--compare-only]
questions_test.jsonl is refused unless --allow-test is given.
--offline-check stops after the imports and the local collection setup,
before any Voyage call. --cache-only never calls Voyage and never reads the
production .env: every Voyage result must already be cached.
"""
import argparse
import atexit
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
#: The free-tier limits are per minute, so a longer single wait never helps.
BACKOFF_429_MAX_S = 65.0

KS = (1, 3, 5, 8)

#: Each variant is scored exactly like the baseline. Fields:
#:   index         "full" = production chunks; "no-refs" = reference-list
#:                 pages dropped, mixed pages cut at the References heading
#:   search_limit  None = production SEARCH_LIMIT (8)
#:   rerank        "production" = rerank_results with RERANK_ENABLED=true
#:                 (vector top 5, first 400 chars); "off" = rerank_results
#:                 with RERANK_ENABLED=false (vector top 5 unchanged);
#:                 "full" = rerank the vector top rerank_n on full chunk text
#:   rerank_n      for "full": how many vector hits are reranked
#:   max_context   None = production MAX_CONTEXT (4000)
#:   network       False = every Voyage result must come from the cache; a
#:                 cache miss stops the run instead of calling Voyage
#:   after         the variant whose cached Voyage results this one reuses
VARIANTS = {
    "baseline": dict(index="full", search_limit=None, rerank="production", max_context=None,
                     network=True, description="Production retrieval, reranking on."),
    "rerank-off": dict(index="full", search_limit=None, rerank="off", max_context=None,
                       network=False, after="baseline",
                       description="Production with RERANK_ENABLED=false: vector top 5, top 4, "
                                   "_select_evidence. Computed from the baseline's cache."),
    "no-refs": dict(index="no-refs", search_limit=None, rerank="production", max_context=None,
                    network=True, after="baseline",
                    description="Reference-list pages not indexed; mixed pages indexed only up to "
                                "the References heading. Otherwise as baseline."),
    # Reranking all 20 on full text needs a paid Voyage tier: one such request
    # measured 6.2K-7.2K tokens, against the free tier's 10K tokens/min, and
    # the free-tier attempt (variant no-refs-rerank-all, since removed) was too
    # slow to finish. Ten full chunks is about half that per request.
    "no-refs-top20-rerank10": dict(index="no-refs", search_limit=20, rerank="full", rerank_n=10,
                                   max_context=None, network=True, after="no-refs",
                                   description="no-refs, vector top 20, rerank the top 10 on full "
                                               "chunk text, keep the top 4."),
    "no-refs-top20-rerank10-ctx6000": dict(index="no-refs", search_limit=20, rerank="full",
                                           rerank_n=10, max_context=6000, network=False,
                                           after="no-refs-top20-rerank10",
                                           description="As no-refs-top20-rerank10, with MAX_CONTEXT 6000."),
}

#: One embedded Qdrant store per index kind; each holds a researchmind_eval collection.
INDEX_STORES = {"full": "qdrant", "no-refs": "qdrant-no-refs"}


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


def configure_environment(rerank_enabled=True, read_voyage_key=True):
    """Must run before any app module is imported. A variant that needs no
    network does not read the production .env at all."""
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
        # Production's switch, read by rerank_results at call time.
        "RERANK_ENABLED": "true" if rerank_enabled else "false",
        # Pacing is done by PacedCachingVoyage below, which also covers the
        # query and rerank calls; the embedder's own between-batch sleep
        # would only double it. Batch composition is unchanged.
        "VOYAGE_EMBED_PACING_SECONDS": "0",
    })
    if read_voyage_key:
        os.environ.update(_read_allowed_keys(SECRETS_ENV))
    else:
        os.environ["VOYAGE_API_KEY"] = ""
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

    def __init__(self, real_client, cache_dir, estimate_tokens, allow_network=True):
        self.real = real_client
        self.allow_network = allow_network and real_client is not None
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

    def _call(self, fn, estimated_tokens, kind):
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
                pause = min(BACKOFF_429_S * (attempt + 1), BACKOFF_429_MAX_S)
                print(f"  Voyage 429 on {kind} (~{estimated_tokens} estimated tokens): backing off "
                      f"{pause:.0f} s, retry {attempt + 1} of {MAX_429_RETRIES}", flush=True)
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
        if not self.allow_network:
            raise SystemExit(f"Voyage {kind} result not in the cache, and Voyage calls are off for "
                             "this run (--cache-only, or a variant that reuses an earlier one's "
                             "results: run that one first).")
        result, latency, tokens = self._call(fetch, estimated_tokens, kind)
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

    # -- per-text embedding records
    #
    # A variant index (e.g. no-refs) batches its chunks differently, so its
    # batch keys miss the cache even when most chunk texts are unchanged.
    # Every embedded text is therefore also kept on its own, and a batch is
    # answered from those records when all its texts are known; only the
    # unknown texts are sent to Voyage, with the same model and input_type.
    def _text_path(self, model, input_type, text):
        key = _digest({"model": model, "input_type": input_type, "text": text})
        return self.cache_dir / "embed_text" / f"{key}.json"

    def _remember_texts(self, model, input_type, texts, embeddings):
        for text, vector in zip(texts, embeddings):
            path = self._text_path(model, input_type, text)
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({"embedding": vector}), encoding="utf-8")

    def _known_text(self, model, input_type, text):
        path = self._text_path(model, input_type, text)
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))["embedding"]
        return None

    def remember_cached_batch(self, model, input_type, texts):
        """Copy one cached batch's vectors into per-text records. Returns
        False when the batch is not cached."""
        key = {"model": model, "input_type": input_type, "texts": list(texts), "kw": {}}
        path = self.cache_dir / "embed" / f"{_digest(key)}.json"
        if not path.exists():
            return False
        record = json.loads(path.read_text(encoding="utf-8"))
        self._remember_texts(model, input_type, texts, record["embeddings"])
        return True

    # -- the two methods production calls
    def embed(self, texts, model, input_type, **kwargs):
        texts = list(texts)
        batch_key = {"model": model, "input_type": input_type, "texts": texts, "kw": kwargs}
        batch_path = self.cache_dir / "embed" / f"{_digest(batch_key)}.json"
        if not batch_path.exists():
            known = {t: self._known_text(model, input_type, t) for t in texts}
            missing = [t for t in dict.fromkeys(texts) if known[t] is None]
            if not missing:
                self.stats["embed_texts_reused"] += len(texts)
                self.last = {"cached": True, "latency_s": None}
                return SimpleNamespace(embeddings=[known[t] for t in texts])
            if len(missing) < len(texts):
                self.stats["embed_texts_reused"] += len(texts) - len(missing)
                record = self._cached(
                    "embed",
                    {"model": model, "input_type": input_type, "texts": missing, "kw": kwargs},
                    lambda: self.real.embed(missing, model=model, input_type=input_type, **kwargs),
                    sum(self.estimate_tokens(t) for t in missing),
                )
                self.stats["embed_texts_new"] += len(missing)
                self._remember_texts(model, input_type, missing, record["embeddings"])
                known.update(zip(missing, record["embeddings"]))
                return SimpleNamespace(embeddings=[known[t] for t in texts])
        record = self._cached(
            "embed", batch_key,
            lambda: self.real.embed(texts, model=model, input_type=input_type, **kwargs),
            sum(self.estimate_tokens(t) for t in texts),
        )
        if input_type == "document":
            self._remember_texts(model, input_type, texts, record["embeddings"])
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


def without_references(app, pages):
    """The no-refs candidate, applied to the loader's pages before the
    production chunker sees them. Each page's cleaned text is classified with
    check_questions.classify_page: a reference page's text is emptied (the
    production 100-character skip then drops it), a mixed page keeps only its
    text before the References heading, and a body page is left untouched,
    so its chunks are byte-identical to the baseline's. Paper-level metadata
    (authors, abstract, keywords) is then built from the remaining text; it
    is payload only and plays no part in retrieval."""
    out, kinds = [], Counter()
    for page in pages:
        kind, body = app.classify_page(app.clean_text(page.get("text", "")))
        kinds[kind] += 1
        if kind == "reference":
            out.append({**page, "text": ""})
        elif kind == "mixed":
            out.append({**page, "text": body})
        else:
            out.append(page)
    return out, dict(kinds)


def build_paper_chunks(app, pdf, index_kind):
    pages = app.extract_pdf_pages(str(pdf))
    if len(pages) > app.limits.max_pages_per_paper():
        raise SystemExit(f"{pdf.name}: {len(pages)} pages exceeds the production cap")
    kinds = None
    if index_kind == "no-refs":
        pages, kinds = without_references(app, pages)
    paper = SimpleNamespace(id=paper_uuid(pdf.name), owner_id=OWNER_ID, title=pdf.name)
    chunks = app.build_chunks(paper, pages)
    return paper, pages, chunks, kinds


def backfill_text_cache(app, pdfs, voyage):
    """Per-text records for every baseline chunk, from the baseline's cached
    batches (same production chunking and batching, so the same keys)."""
    found = 0
    for pdf in pdfs:
        _, _, chunks, _ = build_paper_chunks(app, pdf, "full")
        for batch in app.embedder.batch_by_token_budget([c["text"] for c in chunks]):
            found += voyage.remember_cached_batch(app.embedder.VOYAGE_EMBED_MODEL, "document", batch)
    return found


def index_papers(app, pdfs, voyage, log, index_kind):
    """Index each PDF as production does, skipping papers already present."""
    client = app.vector_store.client
    suffix = "" if index_kind == "full" else f"-{index_kind}"
    manifest_path = CACHE_DIR / f"index_manifest{suffix}.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}

    _collection(app.vector_store.COLLECTION_NAME)
    app.vector_store.create_collection()  # production function, eval name (patched)
    summary = {}
    for pdf in pdfs:
        paper, pages, chunks, kinds = build_paper_chunks(app, pdf, index_kind)
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
        if kinds is not None:
            info["page_classes"] = kinds
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

def rerank_full_text(reranker, question, hits, n):
    """Candidate for the rerank10 variants: rerank the vector top n on full
    chunk text. Same client and model as rerank_results; its top-5 cut
    becomes top n and its 400-character cut is gone. Hits below n are
    dropped, as production drops ranks 6-8."""
    hits = hits[:n]
    if not hits:
        return []
    documents = [hit.payload.get("text", "") for hit in hits]
    reranking = reranker._get_client().rerank(
        query=question, documents=documents, model=reranker.VOYAGE_RERANK_MODEL)
    return [hits[r.index] for r in reranking.results]


def retrieve(app, voyage, question, variant):
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
        limit=variant["search_limit"] or app.ask_stream.SEARCH_LIMIT,
    ).points
    search_s = time.perf_counter() - started
    # keep_live_hits(owner_id, hits) would keep all: every eval paper is indexed.

    voyage.last = None
    if variant["rerank"] == "full":
        ranked = rerank_full_text(app.reranker, question, hits, variant["rerank_n"])
    else:
        # "production" and "off" both run production's rerank_results; the
        # difference is RERANK_ENABLED, set for the process at startup.
        ranked = app.reranker.rerank_results(question, hits)
    rerank_meta = dict(voyage.last) if voyage.last else {"cached": None, "latency_s": None, "scores": []}

    filtered = ranked[:app.ask_stream.TOP_CHUNKS]
    selected = app.ask_stream._select_evidence(filtered)

    out.update(hits=hits, ranked=ranked, rerank_scores=rerank_meta.get("scores", []),
               selected=[hit for hit, _ in selected])
    out["latency_ms"] = {
        "query_embed": (round(embed_meta["latency_s"] * 1000, 1)
                        if embed_meta["latency_s"] is not None else None),
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
        f"Variant `{run['variant']}`: {run['settings']['variant_description']}",
        "",
        "## Settings measured",
        "",
    ]
    lines += [f"- {s}" for s in run["settings"]["summary"]]
    lines += [
        "",
        "## Metrics (answerable items, page level)",
        "",
        "Hit = a retrieved chunk is on any gold page of any gold paper (`cross-09`: any page of a "
        "gold paper). k counts chunks, as production does. `context` = passages handed to the "
        "model. " + run["settings"]["rerank_stage_note"],
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
    score = lambda v: "n/a" if v is None else f"{v:.3f}"  # noqa: E731
    for row in u["items"]:
        lines.append(f"| {row['id']} | {score(row['top_rerank_score'])} | {score(row['top_vector_score'])} | "
                     f"{row['top_page']} ({row['top_page_class']}) |")
    a = u["answerable_top_rerank"]
    if a:
        lines += [
            "",
            f"Answerable items' top rerank score: min {a['min']:.3f}, p25 {a['p25']:.3f}, "
            f"median {a['median']:.3f}, p75 {a['p75']:.3f}, max {a['max']:.3f} (n={a['n']}). "
            f"Answerable items whose top rerank score is below the highest unanswerable top score: "
            f"{u['answerable_below_max_unanswerable']} of {a['n']}.",
        ]
    else:
        lines += ["", "No rerank scores in this variant (no rerank call)."]
    lines += [
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


# --------------------------------------------------------------- compare

def write_compare(split):
    """results/compare-<split>.md over every variant run present for the split."""
    runs = []
    for name in VARIANTS:
        path = RESULTS_DIR / f"{name}-{split}.json"
        if path.exists():
            runs.append((name, json.loads(path.read_text(encoding="utf-8"))))
    if not runs:
        return

    def answerable(run):
        return [it for it in run["items"] if it["type"] != "unanswerable"]

    def cross_ctx_share(run):
        return mean(it["metrics"]["context"].get("gold_paper_share")
                    for it in answerable(run) if it["type"] == "cross-paper")

    def calls(run):
        c = run.get("voyage_calls", {})
        return (c.get("rerank_api", 0) + c.get("rerank_cached", 0), c.get("rerank_api", 0),
                c.get("embed_api", 0))

    lines = [
        f"# Retrieval variants compared: {split} set",
        "",
        "R@k and MRR@5 are on the ranked list the context is taken from (the rerank stage; for "
        "`rerank-off` the vector top 5). Context hit = a gold page is among the passages handed to "
        "the model. All metrics over answerable items, page level (`cross-09` at paper level). "
        "Each run's own file has the vector-stage numbers and page classes.",
        "",
        "| Run | R@1 | R@3 | R@5 | MRR@5 | Context hit | Cross-paper gold-paper share in context "
        "| Reference share in context | Rerank requests (new Voyage calls) | New Voyage embed calls | Wall time |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name, run in runs:
        k = run["metrics"]["rerank"]["all"]
        requests, new_rerank, new_embed = calls(run)
        lines.append(
            f"| {name} | {fmt(k['recall@1'])} | {fmt(k['recall@3'])} | {fmt(k['recall@5'])} | "
            f"{fmt(k['mrr@5'], False)} | {fmt(run['metrics']['context']['all']['hit'])} | "
            f"{fmt(cross_ctx_share(run))} | {fmt(run['page_class_share']['context'].get('reference', 0))} | "
            f"{requests} ({new_rerank}) | {new_embed} | {run['timing']['total_s']:.0f} s |")

    types = sorted({it["type"] for _, run in runs for it in answerable(run)})
    lines += ["", "## Per type", "",
              "| Type | Run | n | R@1 | R@3 | R@5 | MRR@5 | Context hit |",
              "|---|---|---|---|---|---|---|---|"]
    for t in types:
        for name, run in runs:
            k = run["metrics"]["rerank"].get(t)
            c = run["metrics"]["context"].get(t)
            if not k:
                continue
            lines.append(f"| {t} | {name} | {k['n']} | {fmt(k['recall@1'])} | {fmt(k['recall@3'])} | "
                         f"{fmt(k['recall@5'])} | {fmt(k['mrr@5'], False)} | {fmt(c['hit'])} |")

    cross_ids = sorted({it["id"] for _, run in runs for it in answerable(run) if it["type"] == "cross-paper"})
    lines += ["", "## Cross-paper items: share of gold papers in the context", "",
              "| id | " + " | ".join(name for name, _ in runs) + " |",
              "|---|" + "---|" * len(runs)]
    for cid in cross_ids:
        cells = []
        for _, run in runs:
            it = next((i for i in run["items"] if i["id"] == cid), None)
            cells.append(fmt(it["metrics"]["context"].get("gold_paper_share")) if it else "n/a")
        lines.append(f"| {cid} | " + " | ".join(cells) + " |")

    base = dict(runs).get("baseline")
    lines += ["", "## Flips versus baseline", ""]
    if base is None:
        lines.append("No baseline run for this split.")
    else:
        base_items = {it["id"]: it for it in answerable(base)}

        def flips(run, get):
            gained, lost = [], []
            for it in answerable(run):
                before = base_items.get(it["id"])
                if before is None:
                    continue
                was, now = bool(get(before)), bool(get(it))
                if now and not was:
                    gained.append(it["id"])
                elif was and not now:
                    lost.append(it["id"])
            return gained, lost

        lines += ["| Run | Context: miss → hit | Context: hit → miss | R@1: miss → hit | R@1: hit → miss |",
                  "|---|---|---|---|---|"]
        for name, run in runs:
            if name == "baseline":
                continue
            cg, cl = flips(run, lambda it: it["metrics"]["context"]["hit"])
            rg, rl = flips(run, lambda it: it["metrics"]["rerank"]["recall@1"])
            show = lambda ids: ", ".join(ids) or "none"  # noqa: E731
            lines.append(f"| {name} | {show(cg)} | {show(cl)} | {show(rg)} | {show(rl)} |")
    lines.append("")
    (RESULTS_DIR / f"compare-{split}.md").write_text("\n".join(lines), encoding="utf-8")


# ------------------------------------------------------------------- main

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--questions", default=str(EVAL_DIR / "questions_dev.jsonl"))
    parser.add_argument("--variant", choices=list(VARIANTS), default="baseline")
    parser.add_argument("--run-name", help="default: <variant>-<split>, e.g. baseline-dev")
    parser.add_argument("--papers", default=str(DEFAULT_PAPERS))
    parser.add_argument("--allow-test", action="store_true",
                        help="permit questions_test.jsonl (final comparison only)")
    parser.add_argument("--offline-check", action="store_true",
                        help="stop after imports and local collection setup, before any Voyage call")
    parser.add_argument("--cache-only", action="store_true",
                        help="never call Voyage or read the .env; stop on a cache miss")
    parser.add_argument("--compare-only", action="store_true",
                        help="only rewrite results/compare-<split>.md from the existing result files")
    args = parser.parse_args(argv)

    if Path(args.questions).name == "questions_test.jsonl" and not args.allow_test:
        raise SystemExit("questions_test.jsonl is held out; pass --allow-test for the final run")
    split = Path(args.questions).stem.replace("questions_", "")
    if args.compare_only:
        write_compare(split)
        return
    variant = dict(VARIANTS[args.variant])
    if args.cache_only:
        variant["network"] = False
    run_name = args.run_name or f"{args.variant}-{split}"
    if variant.get("after") and not (RESULTS_DIR / f"{variant['after']}-{split}.json").exists():
        raise SystemExit(f"Run --variant {variant['after']} first: {args.variant} reuses its cached results.")

    started_wall = time.perf_counter()
    started_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    log = lambda msg: print(msg, flush=True)  # noqa: E731

    configure_environment(rerank_enabled=variant["rerank"] != "off",
                          read_voyage_key=variant["network"])

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
    from app.services.text_cleaner import clean_text
    import check_questions

    # Replace the shared Qdrant client with an embedded one, in every loaded
    # app module that imported it by name (vector_store, index_document,
    # ask_stream, ...), before any Qdrant call is made. The client built at
    # import time saw an empty QDRANT_URL, so it was never a Cloud client and
    # it is never used.
    qdrant_dir = CACHE_DIR / INDEX_STORES[variant["index"]]
    qdrant_dir.mkdir(parents=True, exist_ok=True)
    local_client = QdrantClient(path=str(qdrant_dir))
    # Closed at exit on every path, including a SystemExit mid-run.
    atexit.register(local_client.close)
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
        return

    real_client = (voyageai.Client(api_key=os.environ["VOYAGE_API_KEY"], **voyage_client_options())
                   if variant["network"] else None)
    voyage = PacedCachingVoyage(real_client, CACHE_DIR, embedder.estimate_tokens,
                                allow_network=variant["network"])
    embedder._client = voyage
    reranker._client = voyage
    assert reranker._rerank_enabled() == (variant["rerank"] != "off")
    if variant["max_context"]:
        # _select_evidence reads the module constant at call time.
        ask_stream.MAX_CONTEXT = variant["max_context"]
    search_limit = variant["search_limit"] or ask_stream.SEARCH_LIMIT

    app = SimpleNamespace(
        ask_stream=ask_stream, build_chunks=_build_chunks_for_paper, limits=limits,
        embedder=embedder, reranker=reranker, vector_store=vector_store,
        is_library_question=is_library_question, extract_pdf_pages=extract_pdf_pages,
        Filter=Filter, FieldCondition=FieldCondition, MatchValue=MatchValue,
        PointStruct=PointStruct, classify_page=check_questions.classify_page,
        clean_text=clean_text)

    pdfs = sorted(Path(args.papers).glob("*.pdf"))
    papers = check_questions.load_papers(args.papers)
    page_kinds = {(name, p.number): p.kind for name, pages in papers.items() for p in pages}

    log(f"Variant {args.variant}: {variant['description']}")
    t0 = time.perf_counter()
    if variant["index"] != "full":
        log(f"  {backfill_text_cache(app, pdfs, voyage)} cached baseline batches available for reuse")
    log(f"Indexing {len(pdfs)} papers into {EVAL_COLLECTION} ({INDEX_STORES[variant['index']]})")
    index_summary = index_papers(app, pdfs, voyage, log, variant["index"])
    index_s = time.perf_counter() - t0

    questions = [json.loads(line) for line in
                 Path(args.questions).read_text(encoding="utf-8").splitlines() if line.strip()]
    log(f"Running {len(questions)} questions")
    t0 = time.perf_counter()
    items = []
    for n, q in enumerate(questions, 1):
        r = retrieve(app, voyage, q["question"], variant)
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
                "rerank": stage_metrics(q, rer, has_k8=variant["rerank"] == "full" and variant["rerank_n"] >= 8),
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
    answerable_top = [s for s in answerable_top if s is not None]
    max_unans = max((r["top_rerank_score"] for r in unans_rows if r["top_rerank_score"] is not None),
                    default=None)
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
    rerank_lines = {
        "production": (f"Rerank (RERANK_ENABLED=true): vector top {reranker.MAX_RERANK} only, Voyage "
                       f"{reranker.VOYAGE_RERANK_MODEL} on each chunk's first 400 chars. With the default "
                       "RERANK_ENABLED=false production keeps the vector top 5 unchanged."),
        "off": ("Rerank off (RERANK_ENABLED=false): rerank_results keeps the vector top "
                f"{reranker.MAX_RERANK} unchanged; no rerank call."),
        "full": (f"Rerank (candidate, not production): vector top {variant.get('rerank_n')} of "
                 f"{search_limit}, Voyage {reranker.VOYAGE_RERANK_MODEL} on the full chunk text. "
                 "Reranking all 20 needs a paid Voyage tier: one 20-chunk request measured "
                 "6.2K-7.2K tokens against the free tier's 10K tokens/min."),
    }
    stage_notes = {
        "production": ("The rerank stage has only 5 items (production cuts to the vector top 5 before "
                       "reranking), so its recall@8 is n/a and its recall@5 equals the vector stage's."),
        "off": ("With reranking off the rerank stage is the vector top 5 unchanged, so its recall@8 "
                "is n/a."),
        "full": (f"The rerank stage reorders the vector top {variant.get('rerank_n')}; vector ranks "
                 f"{(variant.get('rerank_n') or 0) + 1}-{search_limit} are dropped."),
    }
    pages_line = ("Pages: PyMuPDF physical pages from 1, clean_text per page, pages under 100 chars "
                  "skipped; RecursiveCharacterTextSplitter 1500 chars, 300 overlap; payload paper = file name.")
    if variant["index"] == "no-refs":
        pages_line += (" Candidate: reference-list pages not indexed, mixed pages indexed only up to "
                       "the References heading (check_questions.classify_page).")
    run = {
        "run_name": run_name,
        "variant": args.variant,
        "started_utc": started_utc,
        "questions_file": f"eval/{Path(args.questions).name}",
        "collection": EVAL_COLLECTION,
        "owner_id": OWNER_ID,
        "settings": {
            "variant_description": variant["description"],
            "rerank_stage_note": stage_notes[variant["rerank"]],
            "vector_store": f"qdrant embedded local mode (exact search), eval/cache/{INDEX_STORES[variant['index']]}",
            "index": variant["index"],
            "embed_model": embedder.VOYAGE_EMBED_MODEL,
            "rerank_model": reranker.VOYAGE_RERANK_MODEL,
            "search_limit": search_limit,
            "rerank_mode": variant["rerank"],
            "max_rerank": variant["rerank_n"] if variant["rerank"] == "full" else reranker.MAX_RERANK,
            "rerank_doc_chars": None if variant["rerank"] == "full" else 400,
            "top_chunks": ask_stream.TOP_CHUNKS,
            "max_context_chars": ask_stream.MAX_CONTEXT,
            "rerank_enabled": variant["rerank"] != "off",
            "keep_live_hits": "not called (needs Postgres); all eval papers are indexed, so it keeps every hit",
            "summary": [
                pages_line,
                f"Embeddings: Voyage {embedder.VOYAGE_EMBED_MODEL}, input_type document for chunks and "
                "query for the question; Qdrant 1024-d cosine.",
                f"Search: query_points limit {search_limit}, owner_id filter; keep_live_hits "
                "skipped (would keep every hit here).",
                rerank_lines[variant["rerank"]],
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
    (RESULTS_DIR / f"{run_name}.json").write_text(json.dumps(run, indent=1), encoding="utf-8")
    (RESULTS_DIR / f"{run_name}.md").write_text(markdown_summary(run), encoding="utf-8")
    write_compare(split)
    log(f"Done in {total_s:.0f} s. Results: eval/results/{run_name}.json and .md, "
        f"eval/results/compare-{split}.md")


if __name__ == "__main__":
    main()
