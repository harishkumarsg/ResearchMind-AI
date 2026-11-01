"""Golden baseline for the PDF path that indexing uses: extract_pdf_pages()
-> clean_text() -> create_chunks(), driven through
app.api.index_document._build_chunks_for_paper() so the page and chunk_id
assignment is the real one, not a re-implementation.

Shared by tests/test_pdf_pipeline_golden.py and the command below, so the
values written and the values checked come from the same function:

    python -m tests.pdf_golden --print   # any environment; writes nothing
    python -m tests.pdf_golden --write   # target environment only (see TARGET_ENVIRONMENT)

Golden values are only meaningful for the environment that produced them.
--write refuses to run anywhere else, and the golden test skips on other
platforms and fails on the target platform when the library versions drift.
"""
import argparse
import hashlib
import json
import os
import platform
import sys
import uuid
from importlib import metadata
from pathlib import Path

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import tests  # noqa: E402,F401 — hermetic bootstrap; must precede every app import

import fitz  # noqa: E402

from app.api.index_document import _build_chunks_for_paper  # noqa: E402
from app.db.models import Paper  # noqa: E402
from app.services.pdf_loader import extract_pdf_pages  # noqa: E402
from app.services.text_cleaner import clean_text  # noqa: E402
from tests.declared_requirements import versions_equal  # noqa: E402

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "pdf"
MANIFEST_PATH = FIXTURE_DIR / "manifest.json"
GOLDEN_PATH = FIXTURE_DIR / "golden_linux_x86_64_cp311.json"

PLATFORM_KEYS = ("system", "machine", "implementation", "python")
LIBRARY_KEYS = ("PyMuPDF", "langchain-text-splitters")

#: The environment golden values must be generated and checked in. The
#: platform is the one Railway builds and CI runs. The two library versions
#: are production's as reported from the 2026-10-27 Railway build output
#: during the Phase 4 review — medium confidence until confirmed directly
#: from Railway. requirements.txt pins neither, which is why they are named
#: here: they decide what extraction and chunking produce.
TARGET_ENVIRONMENT = {
    "system": "Linux",
    "machine": "x86_64",
    "implementation": "CPython",
    "python": "3.11",
    "PyMuPDF": "2.0",
    "langchain-text-splitters": "1.1.3",
}

# Fixed identifiers: they reach the chunk payload but not the recorded
# values, and fixing them keeps every run identical.
_FIXTURE_OWNER_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")
_FIXTURE_PAPER_ID = uuid.UUID("00000000-0000-4000-8000-000000000002")


def _installed_version(distribution):
    try:
        return metadata.version(distribution)
    except metadata.PackageNotFoundError:
        return None


def current_environment():
    return {
        "system": platform.system(),
        "machine": platform.machine(),
        "implementation": platform.python_implementation(),
        "python": "%d.%d" % sys.version_info[:2],
        "PyMuPDF": _installed_version("PyMuPDF"),
        "langchain-text-splitters": _installed_version("langchain-text-splitters"),
    }


def informational_environment():
    """Recorded alongside the golden values for diagnosis; never matched."""
    return {
        "python_full": platform.python_version(),
        "mupdf": getattr(fitz, "mupdf_version", None) or getattr(fitz, "VersionFitz", None),
        "langchain-core": _installed_version("langchain-core"),
    }


def environment_differences(actual, expected, keys):
    differences = []
    for key in keys:
        want, got = expected.get(key), actual.get(key)
        same = (
            versions_equal(str(want), str(got))
            if key in LIBRARY_KEYS and want is not None and got is not None
            else want == got
        )
        if not same:
            differences.append(f"{key}: expected {want}, found {got}")
    return differences


def load_manifest():
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def fixture_names():
    return sorted(load_manifest()["fixtures"])


def _sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _fixture_paper(name):
    return Paper(
        id=_FIXTURE_PAPER_ID,
        owner_id=_FIXTURE_OWNER_ID,
        title=name,
        content_hash="0" * 64,
        storage_path=f"{_FIXTURE_OWNER_ID}/{_FIXTURE_PAPER_ID}/original.pdf",
        file_size_bytes=0,
        status="uploaded",
    )


def extract(name):
    """Runs the real pipeline on one fixture and returns its full output:
    raw page text, cleaned page text, chunk payloads and each chunk's start
    offset within its cleaned page (-1 if it is not a slice of that page)."""
    path = FIXTURE_DIR / name
    pages = extract_pdf_pages(str(path))
    cleaned = {page["page"]: clean_text(page.get("text", "")) for page in pages}
    chunks = _build_chunks_for_paper(_fixture_paper(name), pages)

    offsets, search_from = [], {}
    for chunk in chunks:
        page = chunk["page"]
        start = cleaned.get(page, "").find(chunk["text"], search_from.get(page, 0))
        if start >= 0:
            search_from[page] = start + 1
        offsets.append(start)

    return {
        "fixture_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "pages": pages,
        "cleaned": cleaned,
        "chunks": chunks,
        "offsets": offsets,
    }


def summarize(result):
    """The recorded form of extract()'s output: lengths, hashes, offsets
    and the page/chunk_id mapping. Text itself is not stored."""
    return {
        "fixture_sha256": result["fixture_sha256"],
        "page_count": len(result["pages"]),
        "pages": [
            {
                "page": page["page"],
                "text_length": len(page["text"]),
                "text_sha256": _sha256_text(page["text"]),
                "cleaned_length": len(result["cleaned"][page["page"]]),
                "cleaned_sha256": _sha256_text(result["cleaned"][page["page"]]),
            }
            for page in result["pages"]
        ],
        "chunk_count": len(result["chunks"]),
        "chunks": [
            {
                "page": chunk["page"],
                "chunk_id": chunk["chunk_id"],
                "chunk_count": chunk["chunk_count"],
                "total_pages": chunk["total_pages"],
                "start": start,
                "length": len(chunk["text"]),
                "sha256": _sha256_text(chunk["text"]),
            }
            for chunk, start in zip(result["chunks"], result["offsets"])
        ],
    }


def observe_all():
    return {name: summarize(extract(name)) for name in fixture_names()}


def compare(observed, expected, path=""):
    """Every difference between two recorded values, as readable lines.
    An empty list means identical."""
    if isinstance(expected, dict) and isinstance(observed, dict):
        differences = []
        for key in sorted(set(expected) | set(observed)):
            where = f"{path}.{key}" if path else str(key)
            if key not in observed:
                differences.append(f"{where}: missing (expected {expected[key]!r})")
            elif key not in expected:
                differences.append(f"{where}: unexpected (found {observed[key]!r})")
            else:
                differences.extend(compare(observed[key], expected[key], where))
        return differences
    if isinstance(expected, list) and isinstance(observed, list):
        differences = []
        if len(expected) != len(observed):
            differences.append(f"{path}: expected {len(expected)} items, found {len(observed)}")
        for index, (got, want) in enumerate(zip(observed, expected)):
            differences.extend(compare(got, want, f"{path}[{index}]"))
        return differences
    return [] if observed == expected else [f"{path}: expected {expected!r}, found {observed!r}"]


def golden_document():
    return {
        "generated_by": "python -m tests.pdf_golden --write",
        "environment": current_environment(),
        "informational": informational_environment(),
        "fixtures": observe_all(),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="PDF pipeline golden values.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--print", action="store_true", help="print values for this environment")
    mode.add_argument("--write", action="store_true", help=f"write {GOLDEN_PATH.name} (target environment only)")
    args = parser.parse_args(argv)

    if args.print:
        print(json.dumps(golden_document(), indent=2, sort_keys=True))
        return 0

    mismatch = environment_differences(current_environment(), TARGET_ENVIRONMENT, PLATFORM_KEYS + LIBRARY_KEYS)
    if mismatch:
        print("Refusing to write golden values outside the target environment:\n  " + "\n  ".join(mismatch))
        return 2
    manifest = load_manifest()["fixtures"]
    document = golden_document()
    tampered = [n for n, v in document["fixtures"].items() if v["fixture_sha256"] != manifest[n]["sha256"]]
    if tampered:
        print("Refusing to write: fixtures differ from manifest.json: " + ", ".join(tampered))
        return 2
    GOLDEN_PATH.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote {GOLDEN_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
