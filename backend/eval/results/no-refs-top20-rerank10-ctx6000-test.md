# Retrieval eval: no-refs-top20-rerank10-ctx6000-test

Run 2026-11-24 18:17 UTC on `eval/questions_test.jsonl` (26 questions: 24 answerable, 2 unanswerable). Collection `researchmind_eval`. Wall time 925 s, of which 907 s was free-tier pacing.

Variant `no-refs-top20-rerank10-ctx6000`: As no-refs-top20-rerank10, with MAX_CONTEXT 6000.

## Settings measured

- Pages: PyMuPDF physical pages from 1, clean_text per page, pages under 100 chars skipped; RecursiveCharacterTextSplitter 1500 chars, 300 overlap; payload paper = file name. Candidate: reference-list pages not indexed, mixed pages indexed only up to the References heading (check_questions.classify_page).
- Embeddings: Voyage voyage-4, input_type document for chunks and query for the question; Qdrant 1024-d cosine.
- Search: query_points limit 20, owner_id filter; keep_live_hits skipped (would keep every hit here).
- Rerank (candidate, not production): vector top 10 of 20, Voyage rerank-3 on the full chunk text. Reranking all 20 needs a paid Voyage tier: one 20-chunk request measured 6.2K-7.2K tokens against the free tier's 10K tokens/min.
- Vector search: Qdrant embedded local mode (exact search). At this collection size Qdrant Cloud generally searches exactly too, so rankings should match; not verified against Cloud.
- Context: top 4 after rerank, _select_evidence within 6000 chars.

## Metrics (answerable items, page level)

Hit = a retrieved chunk is on any gold page of any gold paper (`cross-09`: any page of a gold paper). k counts chunks, as production does. `context` = passages handed to the model. The rerank stage reorders the vector top 10; vector ranks 11-20 are dropped.

| Stage | Type | n | R@1 | R@3 | R@5 | R@8 | MRR@5 | MRR | paper-hit@5 |
|---|---|---|---|---|---|---|---|---|---|
| vector | all | 24 | 75% | 92% | 96% | 100% | 0.837 | 0.843 | 100% |
| vector | cross-paper | 4 | 75% | 100% | 100% | 100% | 0.875 | 0.875 | 100% |
| vector | factual | 6 | 50% | 67% | 83% | 100% | 0.625 | 0.649 | 100% |
| vector | limitation | 4 | 75% | 100% | 100% | 100% | 0.833 | 0.833 | 100% |
| vector | method | 6 | 100% | 100% | 100% | 100% | 1.000 | 1.000 | 100% |
| vector | results | 4 | 75% | 100% | 100% | 100% | 0.875 | 0.875 | 100% |
| rerank | all | 24 | 75% | 100% | 100% | 100% | 0.854 | 0.854 | 100% |
| rerank | cross-paper | 4 | 50% | 100% | 100% | 100% | 0.708 | 0.708 | 100% |
| rerank | factual | 6 | 83% | 100% | 100% | 100% | 0.889 | 0.889 | 100% |
| rerank | limitation | 4 | 75% | 100% | 100% | 100% | 0.875 | 0.875 | 100% |
| rerank | method | 6 | 83% | 100% | 100% | 100% | 0.889 | 0.889 | 100% |
| rerank | results | 4 | 75% | 100% | 100% | 100% | 0.875 | 0.875 | 100% |

Context stage: a gold page reaches the model for 100% of answerable items (mean 4.0 passages).

## Cross-paper items

| id | gold papers | found in vector top 8 | found in rerank list | found in context |
|---|---|---|---|---|
| cross-01 | 2 | 50% | 50% | 50% |
| cross-03 | 2 | 100% | 100% | 100% |
| cross-06 | 2 | 100% | 100% | 50% |
| cross-07 | 3 | 100% | 100% | 33% |

## Unanswerable items: top rerank score

| id | top rerank score | top vector score | top page (class) |
|---|---|---|---|
| unans-04 | 0.470 | 0.538 | 2602.09850v2.pdf p8 (body) |
| unans-05 | 0.462 | 0.385 | 2602.09850v2.pdf p16 (body) |

Answerable items' top rerank score: min 0.429, p25 0.713, median 0.768, p75 0.850, max 0.935 (n=24). Answerable items whose top rerank score is below the highest unanswerable top score: 1 of 24.

## Retrieved page classes (share of retrieved chunks, all questions)

| Stage | body | mixed | reference |
|---|---|---|---|
| vector | 94% | 6% | 0% |
| rerank | 93% | 7% | 0% |
| context | 91% | 9% | 0% |

## Worst misses

Order: no gold page in the vector top 8 first, then later first-hit rank after rerank, then higher top rerank score (confidently wrong).

- **reasoniad-01** (factual): Which encoder does Reason-IAD use to retrieve category knowledge, and how many categories does it retrieve per image?  
  gold 2602.09850v2.pdf p7,14; first gold hit: vector rank 4, rerank rank 3  
  rerank list: 2602.09850v2.pdf p5 (body), 2602.09850v2.pdf p15 (body), 2602.09850v2.pdf p14 (body), 2602.09850v2.pdf p4 (body), 2602.09850v2.pdf p4 (body), 2602.09850v2.pdf p4 (body), 2602.09850v2.pdf p14 (body), 2602.09850v2.pdf p5 (body), 2602.09850v2.pdf p8 (body), 2602.09850v2.pdf p7 (body)
- **vlmaoi-05** (method): How does VLM-AOI use the language side to cut false alarms from the localization stage?  
  gold ETASR_18859.pdf p3; first gold hit: vector rank 1, rerank rank 3  
  rerank list: ETASR_18859.pdf p1 (body), ETASR_18859.pdf p2 (body), ETASR_18859.pdf p3 (body), ETASR_18859.pdf p6 (mixed), ssrn-5745634.pdf p3 (body), ssrn-5745634.pdf p4 (body), 2503.14162v2.pdf p2 (body), 2603.27179v1.pdf p2 (body), 2603.27179v1.pdf p8 (body), 2603.27179v1.pdf p15 (body)
- **cross-07** (cross-paper): Which of the MLLM-based methods are trained or fine-tuned, and which work without any training?  
  gold 2602.09850v2.pdf p2,10,18; 2503.14162v2.pdf p5; 2603.27179v1.pdf p6; first gold hit: vector rank 1, rerank rank 3  
  rerank list: 2603.27179v1.pdf p2 (body), 2603.27179v1.pdf p2 (body), 2603.27179v1.pdf p6 (body), 2603.27179v1.pdf p2 (body), 2602.09850v2.pdf p28 (body), 2503.14162v2.pdf p5 (body), 2503.14162v2.pdf p5 (body), 2602.09850v2.pdf p7 (body), 2602.09850v2.pdf p7 (body), ssrn-5745634.pdf p11 (body)
- **real-08** (results): In the zero-shot comparison, which model has a higher average image-level accuracy than Qwen2.5-VL-7B with CGRO?  
  gold 2603.27179v1.pdf p6,7; first gold hit: vector rank 2, rerank rank 2  
  rerank list: 2603.27179v1.pdf p8 (body), 2603.27179v1.pdf p7 (body), 2602.09850v2.pdf p7 (body), 2603.27179v1.pdf p7 (body), 2603.27179v1.pdf p15 (body), 2603.27179v1.pdf p7 (body), 2602.09850v2.pdf p6 (body), 2603.27179v1.pdf p7 (body), 2602.09850v2.pdf p8 (body), 2602.09850v2.pdf p7 (body)
- **cross-01** (cross-paper): How do EIAD and the ReAL/CGRO paper differ in the supervision needed to get pixel-level anomaly maps?  
  gold 2503.14162v2.pdf p4; 2603.27179v1.pdf p1,2,6; first gold hit: vector rank 2, rerank rank 2  
  rerank list: 2603.27179v1.pdf p12 (body), 2603.27179v1.pdf p1 (body), 2603.27179v1.pdf p2 (body), 2603.27179v1.pdf p5 (body), 2603.27179v1.pdf p1 (body), 2603.27179v1.pdf p5 (body), 2603.27179v1.pdf p3 (body), 2603.27179v1.pdf p12 (body), 2603.27179v1.pdf p12 (body), 2603.27179v1.pdf p14 (body)

## Latency per stage (ms, from this machine; not production's network)

| Stage | median | p95 | max |
|---|---|---|---|
| query_embed | 326 | 1035 | 1299 |
| vector_search | 5 | 8 | 8 |
| rerank | 421 | 801 | 1167 |

Voyage and rerank latencies are the first (uncached) call's, stored in the cache.

Voyage calls this run: {'embed_cached': 26, 'rerank_api': 26, 'rerank_tokens': 105224}
