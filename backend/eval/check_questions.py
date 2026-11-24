"""
Check the retrieval eval set's gold pages against the PDF text.

Offline and read-only: it reads the five PDFs and the question file, nothing
else. No provider, database or network call, and no .env is loaded. Page
numbers come from the backend's own loader (app.services.pdf_loader,
physical pages from 1) and page text is cleaned with the indexer's
clean_text, so lengths and numbering match what /index-document stores.

Checks, per item:
  a. Every gold page exists, has at least 100 cleaned characters (the
     indexer skips shorter pages) and is not a reference-list page. A page
     that mixes body text with the start of the reference list may be gold
     only if its body text carries the answer.
  b. Hint support: the share of the answer_hint's key content (numbers,
     names, distinctive terms, other content words) found on the gold pages.
  c. BM25 over the paper's pages with question + answer_hint as the query.
     Flag when the top page is not gold and clearly beats the best gold page.
  d. Gold lists longer than 3 pages are too broad; suggest the 1 to 3 pages
     that best carry the hint.
  e. Unanswerable items: flag when some page strongly matches the question.
  f. Library routing: flag a question that the live app's
     is_library_question would capture (it then lists titles, no retrieval).

Only gold fixes that need no judgement are "clear-cut": dropping a short or
reference-list gold page when other gold pages remain, and trimming a broad
list when the kept 1-3 pages carry every piece of hint content the full list
carried and every dropped page is clearly weaker than every kept one (a tie
is a judgement). --apply-fixes writes a copy of the questions with exactly those
fixes, each recorded in the item's "edits" list. Everything else is left for
the author.

Usage, from backend/:
    python eval/check_questions.py [--questions eval/questions.jsonl]
        [--papers DIR] [--report eval/check_report.csv] [--apply-fixes OUT]
"""
import argparse
import csv
import json
import math
import re
import sys
import types
import unicodedata
from collections import Counter
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
BACKEND_DIR = EVAL_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))

from app.services.pdf_loader import extract_pdf_pages  # noqa: E402
from app.services.text_cleaner import clean_text  # noqa: E402

MIN_PAGE_CHARS = 100        # app/api/index_document.py skips shorter pages
MAX_GOLD_PAGES = 3
REF_RATIO = 0.55            # citation-like share that makes a page a reference page
REF_MIN_BODY = 400          # body before a References heading, below which the page is references only
LOW_SUPPORT = 0.5           # item-level hint support below this is flagged
WEAK_PAGE = 0.2             # a gold page this weak, next to a strong one, is flagged
# A trim is clear-cut only if every dropped page is this much weaker than every
# kept page. At 0.10, cross-08 would drop ReAL p4, the method page that states
# the answer most directly; hint overlap is too coarse for a smaller gap.
TRIM_GAP = 0.20
BM25_MARGIN = 1.3           # "clearly beats": top page score / best gold score
UNANS_COVERAGE = 0.6        # unanswerable: share of distinctive question terms on one page
UNANS_MIN_TERMS = 3
UNANS_MAX_DF = 0.3          # a term on more than 30% of corpus pages is not distinctive

STOPWORDS = set("""
a about above across after again against all almost along also although am among an and another any
are around as at be because been before being below between both but by can could did do does doing
done down during each either else even ever every few for from further get gets given had has have
having he her here hers him his how however i if in into is it its itself just less like made make
many may me might more most much must my near neither no nor not now of off often on once one only
onto or other others our ours out over own per rather same several she should since so some such
than that the their theirs them then there these they this those though through thus to too toward
towards under until up upon us use used uses using very via was we were what when where whether which
while who whom whose why will with within without would yet you your
paper papers authors author method methods model models approach proposed report reported reports
reach reaches reached obtain obtains obtained show shown shows say says said give gives
versus overall instead based kind kinds kept keep keeps rises rise falling fall happened happen
drawn beat beats appear appears pass passed call called become becomes list listed
""".split())

# Number words in hints also match the digit ("two" or "2").
_NUMBER_WORDS = ["one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"]


# --------------------------------------------------------------------- text

def normalise(text):
    """Lower-cased text for matching: NFKC, joined line-break hyphens,
    thousands separators removed (127,997 -> 127997), unified minus/times."""
    text = unicodedata.normalize("NFKC", text or "")
    text = (text.replace("−", "-").replace("–", "-")
            .replace("—", "-").replace("×", "x"))
    text = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", text)
    text = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.lower()


def stem(word):
    if len(word) > 5 and word.endswith("ing"):
        return word[:-3]
    if len(word) > 4 and word.endswith("ed"):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def tokens(text):
    return re.findall(r"[a-z0-9]+", normalise(text))


def content_terms(text):
    return [stem(t) for t in tokens(text) if t not in STOPWORDS and len(t) > 1]


def compact(text):
    return re.sub(r"[^a-z0-9.]", "", normalise(text))


# ------------------------------------------------------------------- papers

_CITATION_LINE = re.compile(
    r"(^\s*\[\d{1,3}\])|(^\s*\d{1,3}\.\s+[A-Z])|\bet al\.|arXiv|\bProc\.|Proceedings"
    r"|Conference|\bConf\.|\bIEEE\b|\bJournal\b|\bTrans\.|\bvol\.|\bpp\.|\bdoi\b"
    r"|https?://|\bIn:|\bCVPR\b|\bICCV\b|\bECCV\b|\bNeurIPS\b|\bAAAI\b|\bICLR\b"
    r"|\bICML\b|\b(19|20)\d\d[a-z]?\b"
)
_REF_HEADING = re.compile(r"^\s*(references|bibliography)\s*$", re.I | re.M)


def citation_ratio(text):
    lines = [line for line in text.split("\n") if line.strip()]
    total = sum(len(line) for line in lines)
    cited = sum(len(line) for line in lines if _CITATION_LINE.search(line))
    return cited / total if total else 0.0


def classify_page(text):
    """('body' | 'mixed' | 'reference', text that may hold an answer)."""
    heading = _REF_HEADING.search(text)
    if heading:
        body = text[:heading.start()]
        if len(body.strip()) < REF_MIN_BODY:
            return "reference", ""
        return "mixed", body
    if citation_ratio(text) >= REF_RATIO:
        return "reference", ""
    return "body", text


class Page:
    def __init__(self, number, raw_text):
        self.number = number
        self.text = clean_text(raw_text)
        self.kind, self.body = classify_page(self.text)
        self.indexed = len(self.text.strip()) >= MIN_PAGE_CHARS
        self.eligible = self.indexed and self.kind != "reference"
        self.norm = normalise(self.body)
        self.compact = compact(self.body)
        self.token_set = set(re.findall(r"[a-z0-9]+", self.norm))
        self.stems = Counter(content_terms(self.body))


def load_papers(papers_dir):
    papers = {}
    for pdf in sorted(Path(papers_dir).glob("*.pdf")):
        pages = extract_pdf_pages(str(pdf))
        if not pages:
            raise SystemExit(f"Could not read pages from {pdf.name}")
        papers[pdf.name] = [Page(p["page"], p.get("text", "")) for p in pages]
    if not papers:
        raise SystemExit(f"No PDFs found in {papers_dir}")
    return papers


# --------------------------------------------------------------------- BM25

class BM25:
    def __init__(self, docs, k1=1.5, b=0.75):
        self.docs = docs                       # list of Counter
        self.k1, self.b = k1, b
        self.lengths = [sum(d.values()) for d in docs]
        self.avgdl = (sum(self.lengths) / len(docs)) if docs else 0.0
        self.df = Counter()
        for doc in docs:
            self.df.update(doc.keys())
        self.n = len(docs)

    def idf(self, term):
        df = self.df.get(term, 0)
        return math.log(1 + (self.n - df + 0.5) / (df + 0.5))

    def score(self, query_terms, i):
        doc, dl = self.docs[i], self.lengths[i]
        total = 0.0
        for term in query_terms:
            tf = doc.get(term, 0)
            if tf:
                norm = tf + self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1))
                total += self.idf(term) * tf * (self.k1 + 1) / norm
        return total


def rank_pages(pages, query_terms):
    """[(score, page_number)] over eligible pages, best first."""
    eligible = [p for p in pages if p.eligible]
    bm25 = BM25([p.stems for p in eligible])
    ranked = [(bm25.score(query_terms, i), p.number) for i, p in enumerate(eligible)]
    return sorted(ranked, key=lambda x: (-x[0], x[1]))


# ------------------------------------------------------------ hint support

_DISTINCT_TOKEN = re.compile(r"[\w\[\]][\w\[\].\-+/]*[\w\]]|\w")


def hint_items(hint):
    """Key content of an answer hint as [(kind, value, weight)].

    distinct: names and marked terms (capitals inside, letters with digits,
              brackets, all caps, capitalised words mid-sentence), weight 2
    number:   numbers not inside a name, weight 2 (0.5 for a single digit)
    word:     other content words of 4+ letters, weight 1
    """
    text = unicodedata.normalize("NFKC", hint or "").replace("×", "x")
    text = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", text)
    items, seen = [], set()

    def add(kind, value, weight):
        if value and (kind, value) not in seen:
            seen.add((kind, value))
            items.append((kind, value, weight))

    rest = []
    for match in _DISTINCT_TOKEN.finditer(text):
        raw = match.group(0)
        start = match.start()
        sentence_start = start == 0 or re.search(r"[.:;!?]\s*$", text[:start]) is not None
        for part in re.split(r"[/+]", raw):
            part = part.strip(".-")
            if not part:
                continue
            # "LoRA-based", "SAM-based": the name is the key, the suffix a word
            suffix = re.search(r"(?:-[a-z]+)+$", part)
            if suffix and re.search(r"[A-Z0-9]", part[:suffix.start()]):
                rest.extend(suffix.group(0).strip("-").split("-"))
                part = part[:suffix.start()]
            letters = re.sub(r"[^A-Za-z]", "", part)
            has_digit = any(c.isdigit() for c in part)
            is_number = re.fullmatch(r"\d+(?:\.\d+)?%?", part) is not None
            marked = (
                "[" in part or "_" in part
                or (letters and has_digit and not is_number)
                or (len(letters) >= 2 and letters.isupper())
                or any(c.isupper() for c in part[1:])
                or (part[:1].isupper() and not sentence_start
                    and part.lower() not in STOPWORDS)
            )
            if marked:
                value = compact(part)
                # short names match whole tokens ("SAM" not "same"), unless
                # written with separators ("ViT-L" is "vit l" in the text)
                mode = "sub" if len(value) >= 5 or re.search(r"[^A-Za-z0-9]", part) else "tok"
                add("distinct", (value, mode), 2.0)
            elif "-" in part and not re.fullmatch(r"[\d.]+(?:-\d+)?", part):
                rest.extend(part.split("-"))      # "training-free", "fine-tunes"
            else:
                rest.append(part)

    for part in rest:
        number = re.fullmatch(r"(\d+(?:\.\d+)?)%?", part)
        if number:
            value = number.group(1)
            add("number", value, 0.5 if len(value.replace(".", "")) == 1 else 2.0)
            continue
        word = part.lower()
        if word in _NUMBER_WORDS:
            add("numword", word, 1.0)
        elif word not in STOPWORDS and len(word) >= 4 and word.isalpha():
            add("word", stem(word), 1.0)
    return items


def item_found(item, page):
    kind, value, _ = item
    if kind == "number":
        if "." in value:
            pattern = r"(?<![\d.])" + re.escape(value) + r"0*(?!\d)"
        else:
            pattern = r"(?<![\d.])" + re.escape(value) + r"(?![\d]|\.\d)"
        return re.search(pattern, page.norm) is not None
    if kind == "distinct":
        text, mode = value
        return text in page.compact if mode == "sub" else text in page.token_set
    if kind == "numword":
        digit = str(_NUMBER_WORDS.index(value) + 1)
        return value in page.token_set or digit in page.token_set
    return page.stems.get(value, 0) > 0


def support(items, pages):
    """(share of hint weight found on any of these pages, missing values)."""
    total = sum(w for _, _, w in items)
    if not total:
        return 1.0, []
    found = [it for it in items if any(item_found(it, p) for p in pages)]
    missing = [v[0] if k == "distinct" else v for (k, v, w) in items if (k, v, w) not in found]
    return sum(w for _, _, w in found) / total, missing


def relative_support(items, page, paper_pages):
    """Share of the hint content present in this paper that this page holds."""
    present = [it for it in items if any(item_found(it, p) for p in paper_pages)]
    total = sum(w for _, _, w in present)
    if not total:
        return 0.0
    return sum(w for (k, v, w) in present if item_found((k, v, w), page)) / total


# ----------------------------------------------------------- library check

def load_library_matcher():
    """is_library_question without touching the database or auth.

    library_questions imports the DB session and models at module level.
    Only the pure-regex matcher is needed here, so those two modules are
    replaced by empty stand-ins before the import.
    """
    for name in ("app.db.models", "app.db.session"):
        if name not in sys.modules:
            stub = types.ModuleType(name)
            stub.Paper = None
            stub.session_scope = None
            sys.modules[name] = stub
    from app.services.library_questions import is_library_question
    return is_library_question


# ------------------------------------------------------------------ checks

def fmt_gold(gold):
    if not gold:
        return "[]"
    return "; ".join(f"{g['paper']}:{','.join(str(p) for p in g['pages'])}" for g in gold)


def check_item(q, papers, library_match, corpus):
    reasons, info = [], []
    fixes = []                          # (description, new_gold)
    suggested = [dict(g, pages=list(g["pages"])) for g in q["gold"]]
    clear_gold = [dict(g, pages=list(g["pages"])) for g in q["gold"]]
    items = hint_items(q.get("answer_hint", ""))
    query = list(dict.fromkeys(content_terms(q["question"] + " " + q.get("answer_hint", ""))))

    if library_match(q["question"]):
        reasons.append("is_library_question() captures this question: the live app "
                       "would list all paper titles instead of searching")
    elif q["id"] == "cross-09":
        info.append("is_library_question() does not capture it; the live app "
                    "sends it to retrieval")

    if q["type"] == "unanswerable":
        if q["gold"]:
            reasons.append("unanswerable item has gold pages")
        check_unanswerable(q, corpus, reasons)
        return reasons, info, suggested, fixes

    paper_level = q.get("gold_level") == "paper"
    if paper_level:
        info.append("paper-level gold: evidence pages checked, page ranking (check c) skipped")
    all_gold_pages = []
    bm25_extra = {}                     # gold entry index -> BM25 top page to add
    for gi, entry in enumerate(q["gold"]):
        name = entry["paper"]
        if name not in papers:
            reasons.append(f"{name}: paper not found in the PDF folder")
            continue
        pages = papers[name]
        by_number = {p.number: p for p in pages}
        good = []
        # a. existence, length, reference pages
        for n in entry["pages"]:
            page = by_number.get(n)
            if page is None:
                reasons.append(f"{name} p{n}: page does not exist (paper has {len(pages)})")
                continue
            if not page.indexed:
                reasons.append(f"{name} p{n}: under {MIN_PAGE_CHARS} characters, not indexed")
                continue
            if page.kind == "reference":
                reasons.append(f"{name} p{n}: reference-list page")
                continue
            if page.kind == "mixed":
                s, _ = support(items, [page])
                if relative_support(items, page, [p for p in pages if p.eligible]) < WEAK_PAGE:
                    reasons.append(f"{name} p{n}: page mixes body and references and its "
                                   f"body text holds little of the hint ({s:.2f})")
                    continue
            good.append(page)
        bad = [n for n in entry["pages"] if n not in {p.number for p in good}]
        if bad:
            if good:
                kept = [p.number for p in good]
                fixes.append((f"{name}: dropped unusable gold page(s) {bad}", gi, kept))
                suggested[gi]["pages"] = kept
            else:
                reasons.append(f"{name}: no usable gold page left; needs a new page")
        all_gold_pages.extend(good)
        if not good:
            continue

        eligible = [p for p in pages if p.eligible]
        rel = {p.number: relative_support(items, p, eligible) for p in good}

        # c. BM25
        ranked = rank_pages(pages, query)
        scores = dict((n, s) for s, n in ranked)
        top_score, top_page = ranked[0]
        best_gold = max(good, key=lambda p: scores.get(p.number, 0.0))
        best_gold_score = scores.get(best_gold.number, 0.0)
        if paper_level:
            pass                        # any page of the paper counts; ranking is moot
        elif top_page not in entry["pages"] and top_score >= BM25_MARGIN * max(best_gold_score, 1e-9):
            top_rel = relative_support(items, by_number[top_page], eligible)
            reasons.append(
                f"{name}: BM25 top page p{top_page} ({top_score:.1f}, hint {top_rel:.2f}) "
                f"beats best gold p{best_gold.number} ({best_gold_score:.1f}, "
                f"hint {rel[best_gold.number]:.2f})")
            bm25_extra[gi] = top_page

        # d. broad lists
        if len(good) > MAX_GOLD_PAGES:
            # Best-supported pages first (BM25 breaks ties); keep up to 3 that
            # clearly carry the hint.
            order = sorted(good, key=lambda p: (-rel[p.number], -scores.get(p.number, 0.0)))
            keep = [p for p in order if rel[p.number] >= LOW_SUPPORT][:MAX_GOLD_PAGES]
            dropped = [p for p in good if p not in keep]
            full, _ = support(items, good)
            kept = sorted(p.number for p in keep)
            page_support = ", ".join(f"p{p.number} {rel[p.number]:.2f}" for p in order)
            # Clear-cut only when nothing is lost and the cut is not a tie:
            # every dropped page is clearly weaker than every kept page.
            clear = (keep and support(items, keep)[0] >= full - 1e-9
                     and all(rel[d.number] <= min(rel[k.number] for k in keep) - TRIM_GAP + 1e-9
                             for d in dropped))
            if clear:
                fixes.append((f"{name}: trimmed broad gold {[p.number for p in good]} to {kept} "
                              f"(page support {page_support})", gi, kept))
            else:
                reasons.append(f"{name}: {len(good)} gold pages (over {MAX_GOLD_PAGES}) and no "
                               f"clear 1-3 page cut (page support {page_support})")
            suggested[gi]["pages"] = kept or [p.number for p in order[:MAX_GOLD_PAGES]]
        else:
            # weak extra page next to a strong one: keep or drop is a judgement
            strong = [n for n, r in rel.items() if r >= LOW_SUPPORT]
            weak = [n for n, r in rel.items() if r < WEAK_PAGE]
            if strong and weak:
                reasons.append(f"{name}: gold page(s) {weak} hold little of the hint "
                               f"({', '.join(f'p{n} {rel[n]:.2f}' for n in weak)}) next to "
                               f"{', '.join(f'p{n} {rel[n]:.2f}' for n in strong)}")
                suggested[gi]["pages"] = sorted(set(suggested[gi]["pages"]) - set(weak))

    # b. hint support over all gold pages
    if all_gold_pages:
        s, missing = support(items, all_gold_pages)
        if s < LOW_SUPPORT:
            reasons.append(f"gold pages hold {s:.2f} of the hint's key content; "
                           f"missing: {', '.join(missing[:8])}")

    for gi, page in bm25_extra.items():
        suggested[gi]["pages"] = sorted(set(suggested[gi]["pages"]) | {page})
    for desc, gi, kept in fixes:
        clear_gold[gi]["pages"] = kept
    return reasons, info, suggested, [(d, clear_gold) for d, _, _ in fixes]


def check_unanswerable(q, corpus, reasons):
    pages, bm25 = corpus
    terms = list(dict.fromkeys(content_terms(q["question"])))
    distinct = [t for t in terms if bm25.df.get(t, 0) / bm25.n <= UNANS_MAX_DF]
    if not distinct:
        return
    best = None
    for i, (name, page) in enumerate(pages):
        hits = [t for t in distinct if page.stems.get(t, 0)]
        cover = len(hits) / len(distinct)
        key = (cover, bm25.score(terms, i))
        if best is None or key > best[0]:
            best = (key, name, page.number, hits)
    (cover, score), name, number, hits = best
    if cover >= UNANS_COVERAGE and len(hits) >= UNANS_MIN_TERMS:
        reasons.append(f"unanswerable but {name} p{number} matches {len(hits)}/{len(distinct)} "
                       f"distinctive question terms ({', '.join(hits)})")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--questions", default=str(EVAL_DIR / "questions.jsonl"))
    parser.add_argument("--papers", default=str(BACKEND_DIR.parent.parent / "eval-papers"),
                        help="folder holding the corpus PDFs")
    parser.add_argument("--report", default=str(EVAL_DIR / "check_report.csv"))
    parser.add_argument("--apply-fixes", metavar="OUT",
                        help="write the questions with clear-cut gold fixes applied")
    args = parser.parse_args(argv)

    papers = load_papers(args.papers)
    questions = [json.loads(line) for line in
                 Path(args.questions).read_text(encoding="utf-8").splitlines() if line.strip()]
    library_match = load_library_matcher()
    corpus_pages = [(name, p) for name, pages in papers.items() for p in pages if p.eligible]
    corpus = (corpus_pages, BM25([p.stems for _, p in corpus_pages]))

    rows, fixed_questions = [], []
    counts = Counter()
    for q in questions:
        reasons, info, suggested, fixes = check_item(q, papers, library_match, corpus)
        for edit in q.get("edits", []):
            info.append(f"edited: {edit}")
        # A flag the author has already decided stays in the report with its
        # reasons, but no longer counts as open.
        if q.get("review_note"):
            info.append(f"review ({q.get('decided_by', 'reviewer')}): {q['review_note']}")
        status = ("REVIEWED" if q.get("review_note") else "FLAG") if reasons else "OK"
        counts[status] += 1
        if fixes:
            counts["clear-cut fixes"] += 1
        notes = reasons + [f"clear-cut fix: {d}" for d, _ in fixes] + [f"info: {i}" for i in info]
        rows.append({
            "id": q["id"],
            "status": status,
            "reasons": " | ".join(notes),
            "current_gold": fmt_gold(q["gold"]),
            "suggested_gold": fmt_gold(suggested) if (reasons or fixes) else "",
        })
        fixed = dict(q)
        if fixes:
            fixed["gold"] = fixes[-1][1]
            fixed["edits"] = list(q.get("edits", [])) + [d for d, _ in fixes]
        fixed_questions.append(fixed)

    with open(args.report, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["id", "status", "reasons",
                                                "current_gold", "suggested_gold"],
                                lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)

    if args.apply_fixes:
        with open(args.apply_fixes, "w", encoding="utf-8", newline="\n") as fh:
            for q in fixed_questions:
                fh.write(json.dumps(q, ensure_ascii=False) + "\n")

    print(f"items {len(questions)}: OK {counts['OK']}, FLAG {counts['FLAG']}, "
          f"REVIEWED {counts['REVIEWED']}, with clear-cut fixes {counts['clear-cut fixes']}")
    print(f"report: {args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
