# Retrieval eval: no-refs-top20-rerank10-dev

Run 2026-11-24 17:26 UTC on `eval/questions_dev.jsonl` (38 questions: 35 answerable, 3 unanswerable). Collection `researchmind_eval`. Wall time 1317 s, of which 1286 s was free-tier pacing.

Variant `no-refs-top20-rerank10`: no-refs, vector top 20, rerank the top 10 on full chunk text, keep the top 4.

## Settings measured

- Pages: PyMuPDF physical pages from 1, clean_text per page, pages under 100 chars skipped; RecursiveCharacterTextSplitter 1500 chars, 300 overlap; payload paper = file name. Candidate: reference-list pages not indexed, mixed pages indexed only up to the References heading (check_questions.classify_page).
- Embeddings: Voyage voyage-4, input_type document for chunks and query for the question; Qdrant 1024-d cosine.
- Search: query_points limit 20, owner_id filter; keep_live_hits skipped (would keep every hit here).
- Rerank (candidate, not production): vector top 10 of 20, Voyage rerank-3 on the full chunk text. Reranking all 20 needs a paid Voyage tier: one 20-chunk request measured 6.2K-7.2K tokens against the free tier's 10K tokens/min.
- Vector search: Qdrant embedded local mode (exact search). At this collection size Qdrant Cloud generally searches exactly too, so rankings should match; not verified against Cloud.
- Context: top 4 after rerank, _select_evidence within 4000 chars.

## Metrics (answerable items, page level)

Hit = a retrieved chunk is on any gold page of any gold paper (`cross-09`: any page of a gold paper). k counts chunks, as production does. `context` = passages handed to the model. The rerank stage reorders the vector top 10; vector ranks 11-20 are dropped.

| Stage | Type | n | R@1 | R@3 | R@5 | R@8 | MRR@5 | MRR | paper-hit@5 |
|---|---|---|---|---|---|---|---|---|---|
| vector | all | 35 | 83% | 91% | 97% | 97% | 0.881 | 0.882 | 100% |
| vector | cross-paper | 5 | 40% | 80% | 100% | 100% | 0.617 | 0.617 | 100% |
| vector | factual | 9 | 100% | 100% | 100% | 100% | 1.000 | 1.000 | 100% |
| vector | limitation | 6 | 67% | 67% | 83% | 83% | 0.708 | 0.717 | 100% |
| vector | method | 9 | 100% | 100% | 100% | 100% | 1.000 | 1.000 | 100% |
| vector | results | 6 | 83% | 100% | 100% | 100% | 0.917 | 0.917 | 100% |
| rerank | all | 35 | 86% | 97% | 97% | 97% | 0.914 | 0.914 | 100% |
| rerank | cross-paper | 5 | 60% | 100% | 100% | 100% | 0.800 | 0.800 | 100% |
| rerank | factual | 9 | 89% | 100% | 100% | 100% | 0.944 | 0.944 | 100% |
| rerank | limitation | 6 | 83% | 83% | 83% | 83% | 0.833 | 0.833 | 100% |
| rerank | method | 9 | 89% | 100% | 100% | 100% | 0.944 | 0.944 | 100% |
| rerank | results | 6 | 100% | 100% | 100% | 100% | 1.000 | 1.000 | 100% |

Context stage: a gold page reaches the model for 94% of answerable items (mean 2.5 passages).

## Cross-paper items

| id | gold papers | found in vector top 8 | found in rerank list | found in context |
|---|---|---|---|---|
| cross-02 | 2 | 100% | 100% | 100% |
| cross-04 | 3 | 67% | 100% | 67% |
| cross-05 | 2 | 100% | 100% | 100% |
| cross-08 | 2 | 100% | 100% | 100% |
| cross-09 | 5 | 80% | 60% | 40% |

## Unanswerable items: top rerank score

| id | top rerank score | top vector score | top page (class) |
|---|---|---|---|
| unans-01 | 0.385 | 0.507 | ssrn-5745634.pdf p16 (body) |
| unans-02 | 0.492 | 0.502 | 2503.14162v2.pdf p5 (body) |
| unans-03 | 0.498 | 0.433 | 2503.14162v2.pdf p3 (body) |

Answerable items' top rerank score: min 0.554, p25 0.765, median 0.860, p75 0.889, max 0.938 (n=35). Answerable items whose top rerank score is below the highest unanswerable top score: 0 of 35.

## Retrieved page classes (share of retrieved chunks, all questions)

| Stage | body | mixed | reference |
|---|---|---|---|
| vector | 96% | 4% | 0% |
| rerank | 96% | 4% | 0% |
| context | 97% | 3% | 0% |

## Worst misses

Order: no gold page in the vector top 8 first, then later first-hit rank after rerank, then higher top rerank score (confidently wrong).

- **lmv-09** (limitation): What open challenges do the Language Meets Vision authors acknowledge?  
  gold ssrn-5745634.pdf p17; first gold hit: vector rank 19, rerank rank None  
  rerank list: ssrn-5745634.pdf p3 (body), ETASR_18859.pdf p6 (mixed), 2503.14162v2.pdf p6 (mixed), 2602.09850v2.pdf p3 (body), ssrn-5745634.pdf p4 (body), 2503.14162v2.pdf p2 (body), 2503.14162v2.pdf p2 (body), 2603.27179v1.pdf p15 (body), 2603.27179v1.pdf p2 (body), ssrn-5745634.pdf p16 (body)
- **vlmaoi-04** (method): In VLM-AOI's first stage, how are candidate defect regions found?  
  gold ETASR_18859.pdf p2,3; first gold hit: vector rank 1, rerank rank 2  
  rerank list: ETASR_18859.pdf p1 (body), ETASR_18859.pdf p3 (body), ETASR_18859.pdf p2 (body), ETASR_18859.pdf p2 (body), ETASR_18859.pdf p4 (body), ETASR_18859.pdf p3 (body), 2503.14162v2.pdf p4 (body), ETASR_18859.pdf p3 (body), ETASR_18859.pdf p6 (mixed), ETASR_18859.pdf p5 (body)
- **lmv-01** (factual): Which vision-language models were tried as the first-stage describer in Language Meets Vision?  
  gold ssrn-5745634.pdf p11; first gold hit: vector rank 1, rerank rank 2  
  rerank list: ssrn-5745634.pdf p16 (body), ssrn-5745634.pdf p11 (body), ssrn-5745634.pdf p11 (body), 2602.09850v2.pdf p6 (body), 2602.09850v2.pdf p14 (body), ssrn-5745634.pdf p14 (body), 2603.27179v1.pdf p7 (body), 2603.27179v1.pdf p6 (body), 2503.14162v2.pdf p2 (body), ssrn-5745634.pdf p16 (body)
- **cross-08** (cross-paper): Do Reason-IAD and the ReAL paper have the model write out its reasoning as text?  
  gold 2602.09850v2.pdf p2,5; 2603.27179v1.pdf p2,3,4; first gold hit: vector rank 3, rerank rank 2  
  rerank list: 2603.27179v1.pdf p1 (body), 2602.09850v2.pdf p5 (body), 2602.09850v2.pdf p4 (body), 2602.09850v2.pdf p4 (body), 2603.27179v1.pdf p6 (body), 2602.09850v2.pdf p2 (body), 2602.09850v2.pdf p15 (body), 2603.27179v1.pdf p6 (body), 2602.09850v2.pdf p17 (body), 2602.09850v2.pdf p7 (body)
- **cross-05** (cross-paper): Which source datasets appear both in EIAD's DDQA-set and in the CGRO training set?  
  gold 2503.14162v2.pdf p3; 2603.27179v1.pdf p6; first gold hit: vector rank 4, rerank rank 2  
  rerank list: 2603.27179v1.pdf p7 (body), 2503.14162v2.pdf p3 (body), 2503.14162v2.pdf p3 (body), 2503.14162v2.pdf p4 (body), 2503.14162v2.pdf p3 (body), 2503.14162v2.pdf p2 (body), 2503.14162v2.pdf p3 (body), 2603.27179v1.pdf p15 (body), 2602.09850v2.pdf p14 (body), 2603.27179v1.pdf p13 (body)

## Latency per stage (ms, from this machine; not production's network)

| Stage | median | p95 | max |
|---|---|---|---|
| query_embed | 506 | 1862 | 2229 |
| vector_search | 4 | 7 | 11 |
| rerank | 422 | 1166 | 1477 |

Voyage and rerank latencies are the first (uncached) call's, stored in the cache.

Voyage calls this run: {'embed_cached': 38, 'rerank_api': 38, 'rerank_tokens': 151081}
