# Retrieval evaluation set (draft)

> **Status: AI-drafted, not yet reviewed.** Every question, gold page and answer hint in
> `questions_draft.jsonl` was drafted by Claude Code (`"drafted_by": "claude-code"`).
> The author must review the set against the PDFs before it is used to measure anything,
> correcting or deleting items as needed. Until then, do not report numbers computed from it.

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
