# Retrieval evaluation set (draft)

> **Status: AI-drafted by Claude Code. Gold pages checked automatically against the PDF
> text (check_questions.py). The 5 flagged items were decided by the author on Claude's
> recommendation, after Claude Code read the relevant pages. Not every item was re-read
> by the author; the check confirms answers are on their gold pages but cannot judge
> question wording.**

## Files

| File | Purpose |
|---|---|
| `questions_draft.jsonl` | The original 63 drafted items, unchanged. |
| `questions.jsonl` | Working set, 64 items: the draft plus the edits below and `cross-09`. |
| `questions_dev.jsonl` / `questions_test.jsonl` | 38 / 26 split of `questions.jsonl` by `split_questions.py` (stratified by type, seed 20261124, `cross-09` pinned to dev). Tune on dev only; the test file is for the final reported numbers. |
| `check_questions.py` | Offline check of every item against the PDF text (page existence and length, reference pages, hint support, BM25 page ranking, broad gold lists, unanswerable items, library-question routing). Run from `backend/`: `python eval/check_questions.py`. |
| `check_report.csv` | Its output for `questions.jsonl`: `id, status, reasons, current_gold, suggested_gold`. Status is `OK`, `FLAG` (open) or `REVIEWED` (still flagged, but the item has a `review_note` recording the author's decision). |
| `run_retrieval_eval.py` | Runs the questions through the production retrieval path (and candidate variants, `--variant`) and scores the rankings. See its docstring. |
| `results/` | One `<variant>-<split>.json` and `.md` per run, and `compare-<split>.md` across runs. |

Edits in `questions.jsonl` are listed in each item's `edits` field: ten questions
(`lmv-02/03/04/06/07/08/10`, `real-01/06/09`) now name the paper or method, and `cross-09`
was added. It has `"gold_level": "paper"`: any retrieved page from a gold paper counts as
a hit, and the listed page is only the evidence that the paper uses a VLM. The check
found no clear-cut gold fix (no gold page is short or a reference page, and every broad
list needs a judgement), so no gold page was changed automatically. The five flagged
items (`real-03`, `vlmaoi-02`, `cross-04`, `cross-07`, `cross-08`) were then decided by
the author; each carries `decided_by` and a one-line `review_note`.

This is a labelled question set for evaluating ResearchMind's retrieval: for each question,
does retrieval return chunks from the right paper and page?

## Corpus

Five papers, kept outside the repository and never committed:

| File | Pages | Topic |
|---|---|---|
| `2503.14162v2.pdf` | 6 | EIAD: explainable industrial anomaly detection with an MLLM and the DDQA dataset |
| `2602.09850v2.pdf` | 28 | Reason-IAD: knowledge-guided latent reasoning for anomaly detection |
| `2603.27179v1.pdf` | 15 | ReAL + CGRO: reasoning-driven anomaly localization from image-level labels |
| `ETASR_18859.pdf` | 7 | VLM-AOI: PatchCore + CLIP few-shot PCB defect classification |
| `ssrn-5745634.pdf` | 19 | Language Meets Vision: VLM-guided multimodal anomaly segmentation |

The `paper` field in gold must match these file names exactly.

## File format

`questions_draft.jsonl` holds one JSON object per line:

```json
{"id": "eiad-01",
 "question": "How many images went into the DDQA-set, ...?",
 "type": "factual",
 "gold": [{"paper": "2503.14162v2.pdf", "pages": [3]}],
 "answer_hint": "127,997 images drawn from four existing industrial anomaly detection datasets.",
 "drafted_by": "claude-code"}
```

| Field | Meaning |
|---|---|
| `id` | Unique. The prefix names the paper (`eiad`, `reasoniad`, `real`, `vlmaoi`, `lmv`) or the group (`cross`, `unans`). |
| `question` | Phrased the way a researcher would ask it, in the drafter's own words. |
| `type` | `factual`, `method`, `results`, `limitation`, `cross-paper` or `unanswerable`. |
| `gold` | List of `{paper, pages}`. Pages are 1-based and sorted. `[]` means no paper answers the question. |
| `answer_hint` | At most 20 words, paraphrased, to help a reviewer check an answer. It is not a reference answer for automatic scoring. |
| `drafted_by` | Who drafted the item. Change it, or add a reviewer field, when the item is reviewed. |

Contents: 63 items. Each paper has 10 single-paper items (3 factual, 3 method, 2 results,
2 limitation). There are also 8 cross-paper items, with gold in at least two papers, and
5 unanswerable items.

## Page numbering

Page text was extracted with the backend's own loader,
`app.services.pdf_loader.extract_pdf_pages`, so gold pages use the same numbers the app
stores in each chunk's `page` payload. That loader numbers **physical PDF pages from 1**
(`enumerate(doc, start=1)` over PyMuPDF pages) and ignores printed page labels. For
example, the SSRN preprint has a separate title page, so its printed page "2" is
physical page 3. The indexer (`app/api/index_document.py`) also skips pages whose cleaned
text is under 100 characters. No page in these five PDFs falls under that limit, so every
gold page is indexed. All five papers are within the default 40-page cap.

The extraction ran with PyMuPDF 1.26.3 because the pinned 2.0 was not installed offline.
Page counts and page numbering do not depend on the version. Text could differ slightly.

## How gold was assigned

1. Each question was drafted from one page of the extracted text.
2. Each answer's key terms were searched across all pages of the paper. A page was added
   to gold when it also clearly states the answer, for example the same number in a table
   and in the text, or a method stated in both the abstract and the method section. Pages
   that only mention the topic in passing were not added.
3. **Reference-list pages are never gold.** Pages that hold only references are excluded:
   `2602.09850v2.pdf` 11–13, `2603.27179v1.pdf` 9–11, `ETASR_18859.pdf` 7 and
   `ssrn-5745634.pdf` 18–19. Four pages mix body text with the start of the reference
   list: `2503.14162v2.pdf` 6, `2602.09850v2.pdf` 10, `ETASR_18859.pdf` 6 and
   `ssrn-5745634.pdf` 17. They are gold only when the answer is in their body text, such
   as a conclusion or a table.
4. For cross-paper items, every paper that contributes part of the answer has its own
   `gold` entry.
5. Unanswerable items cover plausible topics that no paper addresses: energy use, X-ray
   or CT data, annotation effort, adversarial robustness and factory deployment. Some of
   their words appear in the corpus only in reference lists or unrelated sentences, for
   example "weld beading" as a defect label. That makes them a test of whether the system
   declines to answer from loosely related text.
6. Questions and hints were checked automatically to share no run of 7 or more
   consecutive words with any paper.

## Known points for the reviewer

- `2602.09850v2.pdf` gives Reason-IAD's one-shot average on Qwen3-VL-8B as 79.43 in
  Table 1 (p6) and 79.73 in the text (p8). `cross-02` gives about 79.4% and lists both
  pages.
- Some gold lists are broad, for example `real-03` and `cross-08`, because the same fact
  appears in the abstract, the method section and the tables. Trim them if you want a
  stricter target.
- The answer hints are summaries. Check them against the cited pages before treating
  them as answers.

## Test-set protocol

Written on 2026-11-24, before any retrieval run used `questions_test.jsonl`. Every
variant so far was designed and compared on `questions_dev.jsonl` only
(`results/compare-dev.md`).

**The test set is run once.** Each run below is made exactly once on
`questions_test.jsonl`, with `run_retrieval_eval.py` as of the commit that adds this
section, and every result is reported, including any that is worse than on dev. After
the test results are seen, nothing is tuned for these numbers: no change to the
variants, the scoring, the questions, the gold pages or the script. A gold or question
error found while reading test results is recorded as a note next to the results, not
fixed in them. The script enforces the "once": it refuses to overwrite a test result
that already exists. A run that stops before writing its result file is resumed with the
same command. Its Voyage results so far are cached, so it continues the same run rather
than starting a new one.

`questions_test.jsonl` is frozen at SHA-256
`947a7ee4eb31ef164afb15de4f9b23c19e9deea13cc1c35b4ad9443dec5755c7` (26 items: 6
factual, 6 method, 4 results, 4 limitation, 4 cross-paper, 2 unanswerable).

Runs reported, in this order:

| Run | What it measures | Why it is reported |
|---|---|---|
| `baseline` | Production retrieval with `RERANK_ENABLED=true`: vector top 8, rerank the top 5 on their first 400 characters, keep 4, 4000-character context. | The reference point. |
| `rerank-off` | Production with `RERANK_ENABLED=false`, the code's default: vector top 5 unchanged. | The other production setting. |
| `no-refs-ctx6000-rerank-off` | Reference-list pages not indexed and mixed pages cut at the References heading; reranking off; 6000-character context. One Voyage request per question. | Candidate that fits Voyage's free tier best. Dev: context hit 97% (baseline 91%). |
| `no-refs-top20-rerank10-ctx6000` | Same index; vector top 20, rerank the top 10 on full chunk text, keep 4; 6000-character context. Two Voyage requests per question. | Candidate with the best dev results. Dev: context hit 97%, cross-paper gold-paper share in context 81% (the other candidate: 71%). |

The dev-only steps `no-refs` and `no-refs-top20-rerank10` (4000-character context) are
not run on the test set. Test results go to `results/<variant>-test.json` and `.md`
and to `results/compare-test.md`, with the same metrics as dev.
