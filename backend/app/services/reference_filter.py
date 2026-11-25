"""
Keep a paper's reference list out of the vector index.

Reference-list chunks match almost any query on the topic (titles, method
names, venues) and crowd answer-bearing passages out of the top hits. The
retrieval eval measured this rule as part of the variant
no-refs-ctx6000-rerank-off (backend/eval/README.md, results/compare-test.md):
together with a 6000-character context it raised the test-set context hit
from 88% (production code, reranking off) to 96%.

The rules are ported from backend/eval/check_questions.classify_page, the
function the eval used, with one safety change: citation density is only
applied inside a reference section, never on its own.

  * A page with a "References" or "Bibliography" heading line starts a
    reference section. The text before the heading is kept when it has at
    least REF_MIN_BODY characters (a conclusion or table above the list);
    otherwise the page is dropped. Text after the heading is dropped.
  * The pages directly after that page are dropped while they stay
    citation-dense (citation_ratio >= REF_RATIO). The first page that is not
    citation-dense ends the section, and it and every later page are kept:
    appendices and supplementary material after the references are indexed.
  * Citation-dense pages anywhere else (a related-work section, a dense
    table of prior results) are never dropped, and a paper without a
    heading loses nothing.

The eval classified every page on its own, so a citation-dense page
outside a reference section would also have been dropped there. On the five
eval papers both rules drop exactly the same text: the implementation check
(eval/results/impl-check-dev.md and impl-check-test.md) found every chunk
byte-identical to the measured index.
"""
import re

from app.services.text_cleaner import clean_text

#: Citation-like share of a page's characters that makes it a reference page.
REF_RATIO = 0.55
#: Body text before a References heading below which the page counts as
#: references only.
REF_MIN_BODY = 400

_CITATION_LINE = re.compile(
    r"(^\s*\[\d{1,3}\])|(^\s*\d{1,3}\.\s+[A-Z])|\bet al\.|arXiv|\bProc\.|Proceedings"
    r"|Conference|\bConf\.|\bIEEE\b|\bJournal\b|\bTrans\.|\bvol\.|\bpp\.|\bdoi\b"
    r"|https?://|\bIn:|\bCVPR\b|\bICCV\b|\bECCV\b|\bNeurIPS\b|\bAAAI\b|\bICLR\b"
    r"|\bICML\b|\b(19|20)\d\d[a-z]?\b"
)
_REF_HEADING = re.compile(r"^\s*(references|bibliography)\s*$", re.I | re.M)

BODY = "body"
MIXED = "mixed"
REFERENCE = "reference"


def citation_ratio(text: str) -> float:
    """Share of the page's non-blank characters on lines that look like a
    citation (numbered entry, et al., venue, year, DOI, URL...)."""
    lines = [line for line in text.split("\n") if line.strip()]
    total = sum(len(line) for line in lines)
    cited = sum(len(line) for line in lines if _CITATION_LINE.search(line))
    return cited / total if total else 0.0


def classify_pages(texts: list) -> list:
    """(kind, kept_text) for each cleaned page text, in order.

    kind is "body" (kept_text is the page), "mixed" (kept_text is the body
    before the References heading) or "reference" (kept_text is "")."""
    out = []
    in_references = False
    for text in texts:
        heading = _REF_HEADING.search(text)
        if heading:
            body = text[:heading.start()]
            if len(body.strip()) < REF_MIN_BODY:
                out.append((REFERENCE, ""))
            else:
                out.append((MIXED, body))
            in_references = True
            continue
        if in_references and citation_ratio(text) >= REF_RATIO:
            out.append((REFERENCE, ""))
            continue
        in_references = False
        out.append((BODY, text))
    return out


def drop_reference_text(pages: list) -> list:
    """The loader's pages with reference-list text removed, for the indexer.

    A reference page keeps its entry with empty text, so page numbers and
    the page count are unchanged and the indexer's short-page skip drops it.
    A mixed page keeps its cleaned text up to the heading. A body page is
    returned untouched, so its chunks are exactly what they were before."""
    kinds = classify_pages([clean_text(page.get("text", "")) for page in pages])
    out = []
    for page, (kind, kept) in zip(pages, kinds):
        if kind == BODY:
            out.append(page)
        else:
            out.append({**page, "text": kept})
    return out
