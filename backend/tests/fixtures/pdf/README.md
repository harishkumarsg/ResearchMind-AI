# PDF fixtures

Used by `tests/test_pdf_pipeline_golden.py` to detect changes in PDF text
extraction, cleaning, chunking and the page / chunk_id mapping that indexing
writes to every vector.

## Licence and provenance

- **Content:** every word was written for this test suite. The documents
  describe imaginary studies and say so on their first page. No published
  paper, private document, user upload or third-party text is used.
- **Fonts:** only the PDF standard fonts (Times, Helvetica, Courier) are
  referenced. Standard fonts are never embedded, so the files contain no font
  software.
- **Generator:** `generate_fixtures.py` (reportlab, invariant mode, so the
  output is byte-for-byte repeatable for a given reportlab version). The
  files were generated with reportlab 4.5.1, the version pinned in
  `requirements.txt`.
- **Licence:** distributed under the repository's MIT licence, like the rest
  of the test suite.

| File | Pages | What it exercises |
|---|---|---|
| `single_column.pdf` | 3 | Long pages that split into several chunks; `Page N` footers that `clean_text` removes; a short final page that indexing skips |
| `two_column.pdf` | 2 | Two columns under a full-width title: reading order across columns |
| `typography.pdf` | 3 | Windows-1252 punctuation, accents and symbols; a hyphenated line break; a small table; a blank page; a monospaced block with runs of spaces |

`manifest.json` records each file's SHA-256, size, page count and per-page
marker strings. The tests check the files against it, so the golden values
can never silently refer to different bytes.

## Golden values

`golden_linux_x86_64_cp311.json` holds exact lengths, hashes and offsets. It
is valid only for the environment named in `tests/pdf_golden.py`
(`TARGET_ENVIRONMENT`: Linux x86_64, CPython 3.11, and production's PyMuPDF
and langchain-text-splitters versions), and is generated there with:

    python -m tests.pdf_golden --write

The command refuses to run in any other environment. On other platforms the
golden test skips; on the target platform it fails if the library versions
differ or if the file has not been generated yet. In that case the failure
message contains the values observed, for review.

Regenerating the PDFs (`python tests/fixtures/pdf/generate_fixtures.py`)
changes their hashes and invalidates the golden values. Use `--check` to
confirm they still reproduce without rewriting anything.
