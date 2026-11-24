# Retrieval eval: no-refs-ctx6000-rerank-off-test

Run 2026-11-24 18:17 UTC on `eval/questions_test.jsonl` (26 questions: 24 answerable, 2 unanswerable). Collection `researchmind_eval`. Wall time 5 s, of which 0 s was free-tier pacing.

Variant `no-refs-ctx6000-rerank-off`: no-refs with RERANK_ENABLED=false and MAX_CONTEXT 6000: vector top 5, top 4, _select_evidence. One Voyage request per question (the query embedding).

## Settings measured

- Pages: PyMuPDF physical pages from 1, clean_text per page, pages under 100 chars skipped; RecursiveCharacterTextSplitter 1500 chars, 300 overlap; payload paper = file name. Candidate: reference-list pages not indexed, mixed pages indexed only up to the References heading (check_questions.classify_page).
- Embeddings: Voyage voyage-4, input_type document for chunks and query for the question; Qdrant 1024-d cosine.
- Search: query_points limit 8, owner_id filter; keep_live_hits skipped (would keep every hit here).
- Rerank off (RERANK_ENABLED=false): rerank_results keeps the vector top 5 unchanged; no rerank call.
- Vector search: Qdrant embedded local mode (exact search). At this collection size Qdrant Cloud generally searches exactly too, so rankings should match; not verified against Cloud.
- Context: top 4 after rerank, _select_evidence within 6000 chars.

## Metrics (answerable items, page level)

Hit = a retrieved chunk is on any gold page of any gold paper (`cross-09`: any page of a gold paper). k counts chunks, as production does. `context` = passages handed to the model. With reranking off the rerank stage is the vector top 5 unchanged, so its recall@8 is n/a.

| Stage | Type | n | R@1 | R@3 | R@5 | R@8 | MRR@5 | MRR | paper-hit@5 |
|---|---|---|---|---|---|---|---|---|---|
| vector | all | 24 | 75% | 92% | 96% | 100% | 0.837 | 0.843 | 100% |
| vector | cross-paper | 4 | 75% | 100% | 100% | 100% | 0.875 | 0.875 | 100% |
| vector | factual | 6 | 50% | 67% | 83% | 100% | 0.625 | 0.649 | 100% |
| vector | limitation | 4 | 75% | 100% | 100% | 100% | 0.833 | 0.833 | 100% |
| vector | method | 6 | 100% | 100% | 100% | 100% | 1.000 | 1.000 | 100% |
| vector | results | 4 | 75% | 100% | 100% | 100% | 0.875 | 0.875 | 100% |
| rerank | all | 24 | 75% | 92% | 96% | n/a | 0.837 | 0.837 | 100% |
| rerank | cross-paper | 4 | 75% | 100% | 100% | n/a | 0.875 | 0.875 | 100% |
| rerank | factual | 6 | 50% | 67% | 83% | n/a | 0.625 | 0.625 | 100% |
| rerank | limitation | 4 | 75% | 100% | 100% | n/a | 0.833 | 0.833 | 100% |
| rerank | method | 6 | 100% | 100% | 100% | n/a | 1.000 | 1.000 | 100% |
| rerank | results | 4 | 75% | 100% | 100% | n/a | 0.875 | 0.875 | 100% |

Context stage: a gold page reaches the model for 96% of answerable items (mean 4.0 passages).

## Cross-paper items

| id | gold papers | found in vector top 8 | found in rerank list | found in context |
|---|---|---|---|---|
| cross-01 | 2 | 50% | 50% | 50% |
| cross-03 | 2 | 100% | 100% | 100% |
| cross-06 | 2 | 100% | 50% | 50% |
| cross-07 | 3 | 100% | 67% | 67% |

## Unanswerable items: top rerank score

| id | top rerank score | top vector score | top page (class) |
|---|---|---|---|
| unans-04 | n/a | 0.538 | 2602.09850v2.pdf p9 (body) |
| unans-05 | n/a | 0.385 | 2603.27179v1.pdf p15 (body) |

No rerank scores in this variant (no rerank call).

## Retrieved page classes (share of retrieved chunks, all questions)

| Stage | body | mixed | reference |
|---|---|---|---|
| vector | 93% | 7% | 0% |
| rerank | 92% | 8% | 0% |
| context | 91% | 9% | 0% |

## Worst misses

Order: no gold page in the vector top 8 first, then later first-hit rank after rerank, then higher top rerank score (confidently wrong).

- **real-02** (factual): How large is the training set used for CGRO, and what kind of labels does it carry?  
  gold 2603.27179v1.pdf p6; first gold hit: vector rank 7, rerank rank None  
  rerank list: 2503.14162v2.pdf p4 (body), 2603.27179v1.pdf p13 (body), 2503.14162v2.pdf p3 (body), ETASR_18859.pdf p3 (body), 2603.27179v1.pdf p15 (body)
- **reasoniad-01** (factual): Which encoder does Reason-IAD use to retrieve category knowledge, and how many categories does it retrieve per image?  
  gold 2602.09850v2.pdf p7,14; first gold hit: vector rank 4, rerank rank 4  
  rerank list: 2602.09850v2.pdf p15 (body), 2602.09850v2.pdf p5 (body), 2602.09850v2.pdf p4 (body), 2602.09850v2.pdf p7 (body), 2602.09850v2.pdf p4 (body)
- **real-09** (limitation): What limitations do the authors of the ReAL/CGRO paper state?  
  gold 2603.27179v1.pdf p15; first gold hit: vector rank 3, rerank rank 3  
  rerank list: 2602.09850v2.pdf p22 (body), 2603.27179v1.pdf p12 (body), 2603.27179v1.pdf p15 (body), 2602.09850v2.pdf p22 (body), 2603.27179v1.pdf p7 (body)
- **real-08** (results): In the zero-shot comparison, which model has a higher average image-level accuracy than Qwen2.5-VL-7B with CGRO?  
  gold 2603.27179v1.pdf p6,7; first gold hit: vector rank 2, rerank rank 2  
  rerank list: 2603.27179v1.pdf p15 (body), 2603.27179v1.pdf p7 (body), 2603.27179v1.pdf p7 (body), 2602.09850v2.pdf p7 (body), 2603.27179v1.pdf p7 (body)
- **vlmaoi-01** (factual): What dataset was VLM-AOI evaluated on, and how many defect classes does it provide?  
  gold ETASR_18859.pdf p3; first gold hit: vector rank 2, rerank rank 2  
  rerank list: ETASR_18859.pdf p4 (body), ETASR_18859.pdf p3 (body), ETASR_18859.pdf p5 (body), ETASR_18859.pdf p6 (mixed), ETASR_18859.pdf p4 (body)

## Latency per stage (ms, from this machine; not production's network)

| Stage | median | p95 | max |
|---|---|---|---|
| query_embed | 326 | 1035 | 1299 |
| vector_search | 3 | 4 | 5 |

Voyage and rerank latencies are the first (uncached) call's, stored in the cache.

Voyage calls this run: {'embed_cached': 26}
