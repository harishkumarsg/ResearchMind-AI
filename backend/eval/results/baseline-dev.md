# Retrieval eval: baseline-dev

Run 2026-11-24 15:57 UTC on `eval/questions_dev.jsonl` (38 questions: 35 answerable, 3 unanswerable). Collection `researchmind_eval`. Wall time 2603 s, of which 2458 s was free-tier pacing.

## Production settings measured

- Pages: PyMuPDF physical pages from 1, clean_text per page, pages under 100 chars skipped; RecursiveCharacterTextSplitter 1500 chars, 300 overlap; payload paper = file name.
- Embeddings: Voyage voyage-4, input_type document for chunks and query for the question; Qdrant 1024-d cosine.
- Search: query_points limit 8, owner_id filter; keep_live_hits skipped (would keep every hit here).
- Rerank (RERANK_ENABLED=true): vector top 5 only, Voyage rerank-3 on each chunk's first 400 chars. With the default RERANK_ENABLED=false production keeps the vector top 5 unchanged.
- Vector search: Qdrant embedded local mode (exact search). At this collection size Qdrant Cloud generally searches exactly too, so rankings should match; not verified against Cloud.
- Context: top 4 after rerank, _select_evidence within 4000 chars.

## Metrics (answerable items, page level)

Hit = a retrieved chunk is on any gold page of any gold paper (`cross-09`: any page of a gold paper). k counts chunks, as production does. The rerank stage has only 5 items (production cuts to the vector top 5 before reranking), so its recall@8 is n/a and its recall@5 equals the vector stage's. `context` = passages handed to the model.

| Stage | Type | n | R@1 | R@3 | R@5 | R@8 | MRR@5 | MRR | paper-hit@5 |
|---|---|---|---|---|---|---|---|---|---|
| vector | all | 35 | 83% | 86% | 94% | 97% | 0.863 | 0.868 | 100% |
| vector | cross-paper | 5 | 40% | 40% | 80% | 100% | 0.490 | 0.523 | 100% |
| vector | factual | 9 | 100% | 100% | 100% | 100% | 1.000 | 1.000 | 100% |
| vector | limitation | 6 | 67% | 67% | 83% | 83% | 0.708 | 0.708 | 100% |
| vector | method | 9 | 100% | 100% | 100% | 100% | 1.000 | 1.000 | 100% |
| vector | results | 6 | 83% | 100% | 100% | 100% | 0.917 | 0.917 | 100% |
| rerank | all | 35 | 83% | 91% | 94% | n/a | 0.869 | 0.869 | 100% |
| rerank | cross-paper | 5 | 60% | 80% | 80% | n/a | 0.700 | 0.700 | 100% |
| rerank | factual | 9 | 100% | 100% | 100% | n/a | 1.000 | 1.000 | 100% |
| rerank | limitation | 6 | 50% | 67% | 83% | n/a | 0.597 | 0.597 | 100% |
| rerank | method | 9 | 100% | 100% | 100% | n/a | 1.000 | 1.000 | 100% |
| rerank | results | 6 | 83% | 100% | 100% | n/a | 0.889 | 0.889 | 100% |

Context stage: a gold page reaches the model for 91% of answerable items (mean 2.8 passages).

## Cross-paper items

| id | gold papers | found in vector top 8 | found in rerank list | found in context |
|---|---|---|---|---|
| cross-02 | 2 | 100% | 100% | 100% |
| cross-04 | 3 | 67% | 33% | 33% |
| cross-05 | 2 | 100% | 100% | 100% |
| cross-08 | 2 | 100% | 50% | 50% |
| cross-09 | 5 | 100% | 80% | 60% |

## Unanswerable items: top rerank score

| id | top rerank score | top vector score | top page (class) |
|---|---|---|---|
| unans-01 | 0.388 | 0.507 | ssrn-5745634.pdf p16 (body) |
| unans-02 | 0.521 | 0.502 | 2603.27179v1.pdf p15 (body) |
| unans-03 | 0.315 | 0.433 | 2602.09850v2.pdf p14 (body) |

Answerable items' top rerank score: min 0.388, p25 0.544, median 0.656, p75 0.804, max 0.917 (n=35). Answerable items whose top rerank score is below the highest unanswerable top score: 8 of 35.

## Retrieved page classes (share of retrieved chunks, all questions)

| Stage | body | mixed | reference |
|---|---|---|---|
| vector | 87% | 5% | 8% |
| rerank | 89% | 4% | 7% |
| context | 92% | 3% | 6% |

## Worst misses

Order: no gold page in the vector top 8 first, then later first-hit rank after rerank, then higher top rerank score (confidently wrong).

- **lmv-09** (limitation): What open challenges do the Language Meets Vision authors acknowledge?  
  gold ssrn-5745634.pdf p17; first gold hit: vector rank None, rerank rank None  
  rerank list: 2503.14162v2.pdf p2 (body), ssrn-5745634.pdf p3 (body), ssrn-5745634.pdf p16 (body), 2603.27179v1.pdf p15 (body), ETASR_18859.pdf p6 (mixed)
- **cross-04** (cross-paper): How is CLIP used across these papers?  
  gold ETASR_18859.pdf p3,4; 2602.09850v2.pdf p7,14; ssrn-5745634.pdf p12; first gold hit: vector rank 6, rerank rank None  
  rerank list: 2602.09850v2.pdf p1 (body), 2602.09850v2.pdf p10 (mixed), 2603.27179v1.pdf p9 (reference), 2603.27179v1.pdf p9 (reference), 2603.27179v1.pdf p10 (reference)
- **reasoniad-10** (limitation): What kinds of images did Reason-IAD get wrong in its reported failure cases?  
  gold 2602.09850v2.pdf p18; first gold hit: vector rank 4, rerank rank 4  
  rerank list: 2603.27179v1.pdf p14 (body), ETASR_18859.pdf p5 (body), 2603.27179v1.pdf p7 (body), 2602.09850v2.pdf p18 (body), 2603.27179v1.pdf p15 (body)
- **reasoniad-07** (results): In the zero-shot MMAD setting, what average accuracy does Reason-IAD reach with Qwen3-VL-8B?  
  gold 2602.09850v2.pdf p7; first gold hit: vector rank 2, rerank rank 3  
  rerank list: 2602.09850v2.pdf p8 (body), 2602.09850v2.pdf p17 (body), 2602.09850v2.pdf p7 (body), 2602.09850v2.pdf p8 (body), 2602.09850v2.pdf p7 (body)
- **lmv-10** (limitation): How sensitive is the Language Meets Vision framework to which VLM writes the stage-1 descriptions?  
  gold ssrn-5745634.pdf p16; first gold hit: vector rank 1, rerank rank 3  
  rerank list: ssrn-5745634.pdf p4 (body), ssrn-5745634.pdf p5 (body), ssrn-5745634.pdf p16 (body), ssrn-5745634.pdf p8 (body), ssrn-5745634.pdf p16 (body)

## Latency per stage (ms, from this machine; not production's network)

| Stage | median | p95 | max |
|---|---|---|---|
| query_embed | 506 | 1862 | 2229 |
| vector_search | 12 | 20 | 24 |
| rerank | 477 | 1366 | 1739 |

Voyage and rerank latencies are the first (uncached) call's, stored in the cache.

Voyage calls this run: {'embed_api': 58, 'embed_tokens': 85605, 'rerank_api': 38, 'rerank_tokens': 26311}
