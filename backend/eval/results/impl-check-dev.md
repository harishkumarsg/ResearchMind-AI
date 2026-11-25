# Retrieval eval: impl-check-dev

Run 2026-11-25 17:21 UTC on `eval/questions_dev.jsonl` (38 questions: 35 answerable, 3 unanswerable). Collection `researchmind_eval`. Wall time 15 s, of which 0 s was free-tier pacing.

Variant `impl-check`: Implementation check, not a new result: the production indexer (reference filter on), production MAX_CONTEXT, RERANK_ENABLED unset (code default). Must reproduce no-refs-ctx6000-rerank-off exactly.

## Implementation check (not a new result)

This run checks that the production code reproduces `no-refs-ctx6000-rerank-off-dev`, the measured run it implements. It is not a new measurement and is not listed in the compare files.

**Result: identical.** Compared: every question's vector, rerank and context lists (paper, page, chunk id, score, rank), its metrics, the aggregate metrics, cross-paper and unanswerable tables, page-class shares, worst misses, the retrieval settings, the per-paper index counts and page classes, and each paper's chunk fingerprint (embedding model and every chunk payload).

| Paper | Chunk fingerprint (this run) | Same as reference |
|---|---|---|
| 2503.14162v2.pdf | `75cac8b01a5cd5fa…` | yes |
| 2602.09850v2.pdf | `5122220cb3c17083…` | yes |
| 2603.27179v1.pdf | `630e52310756a358…` | yes |
| ETASR_18859.pdf | `15a0731c4695ef39…` | yes |
| ssrn-5745634.pdf | `e620691882e746b2…` | yes |

## Settings measured

- Pages: PyMuPDF physical pages from 1, clean_text per page, pages under 100 chars skipped; RecursiveCharacterTextSplitter 1500 chars, 300 overlap; payload paper = file name. Production indexer with its reference filter (app/services/reference_filter.py): the References heading page cut or dropped, then the citation-dense pages directly after it dropped.
- Embeddings: Voyage voyage-4, input_type document for chunks and query for the question; Qdrant 1024-d cosine.
- Search: query_points limit 8, owner_id filter; keep_live_hits skipped (would keep every hit here).
- Rerank off (RERANK_ENABLED=false): rerank_results keeps the vector top 5 unchanged; no rerank call.
- Vector search: Qdrant embedded local mode (exact search). At this collection size Qdrant Cloud generally searches exactly too, so rankings should match; not verified against Cloud.
- Context: top 4 after rerank, _select_evidence within 6000 chars.

## Metrics (answerable items, page level)

Hit = a retrieved chunk is on any gold page of any gold paper (`cross-09`: any page of a gold paper). k counts chunks, as production does. `context` = passages handed to the model. With reranking off the rerank stage is the vector top 5 unchanged, so its recall@8 is n/a.

| Stage | Type | n | R@1 | R@3 | R@5 | R@8 | MRR@5 | MRR | paper-hit@5 |
|---|---|---|---|---|---|---|---|---|---|
| vector | all | 35 | 83% | 91% | 97% | 97% | 0.881 | 0.881 | 100% |
| vector | cross-paper | 5 | 40% | 80% | 100% | 100% | 0.617 | 0.617 | 100% |
| vector | factual | 9 | 100% | 100% | 100% | 100% | 1.000 | 1.000 | 100% |
| vector | limitation | 6 | 67% | 67% | 83% | 83% | 0.708 | 0.708 | 100% |
| vector | method | 9 | 100% | 100% | 100% | 100% | 1.000 | 1.000 | 100% |
| vector | results | 6 | 83% | 100% | 100% | 100% | 0.917 | 0.917 | 100% |
| rerank | all | 35 | 83% | 91% | 97% | n/a | 0.881 | 0.881 | 100% |
| rerank | cross-paper | 5 | 40% | 80% | 100% | n/a | 0.617 | 0.617 | 100% |
| rerank | factual | 9 | 100% | 100% | 100% | n/a | 1.000 | 1.000 | 100% |
| rerank | limitation | 6 | 67% | 67% | 83% | n/a | 0.708 | 0.708 | 100% |
| rerank | method | 9 | 100% | 100% | 100% | n/a | 1.000 | 1.000 | 100% |
| rerank | results | 6 | 83% | 100% | 100% | n/a | 0.917 | 0.917 | 100% |

Context stage: a gold page reaches the model for 97% of answerable items (mean 4.0 passages).

## Cross-paper items

| id | gold papers | found in vector top 8 | found in rerank list | found in context |
|---|---|---|---|---|
| cross-02 | 2 | 100% | 100% | 100% |
| cross-04 | 3 | 67% | 67% | 67% |
| cross-05 | 2 | 100% | 100% | 100% |
| cross-08 | 2 | 100% | 50% | 50% |
| cross-09 | 5 | 80% | 40% | 40% |

## Unanswerable items: top rerank score

| id | top rerank score | top vector score | top page (class) |
|---|---|---|---|
| unans-01 | n/a | 0.507 | ssrn-5745634.pdf p16 (body) |
| unans-02 | n/a | 0.502 | 2603.27179v1.pdf p15 (body) |
| unans-03 | n/a | 0.433 | 2503.14162v2.pdf p3 (body) |

No rerank scores in this variant (no rerank call).

## Retrieved page classes (share of retrieved chunks, all questions)

| Stage | body | mixed | reference |
|---|---|---|---|
| vector | 95% | 5% | 0% |
| rerank | 97% | 3% | 0% |
| context | 97% | 3% | 0% |

## Worst misses

Order: no gold page in the vector top 8 first, then later first-hit rank after rerank, then higher top rerank score (confidently wrong).

- **lmv-09** (limitation): What open challenges do the Language Meets Vision authors acknowledge?  
  gold ssrn-5745634.pdf p17; first gold hit: vector rank None, rerank rank None  
  rerank list: ssrn-5745634.pdf p3 (body), 2603.27179v1.pdf p15 (body), 2503.14162v2.pdf p6 (mixed), ETASR_18859.pdf p6 (mixed), ssrn-5745634.pdf p16 (body)
- **reasoniad-10** (limitation): What kinds of images did Reason-IAD get wrong in its reported failure cases?  
  gold 2602.09850v2.pdf p18; first gold hit: vector rank 4, rerank rank 4  
  rerank list: 2603.27179v1.pdf p15 (body), 2603.27179v1.pdf p14 (body), ETASR_18859.pdf p5 (body), 2602.09850v2.pdf p18 (body), 2603.27179v1.pdf p7 (body)
- **cross-05** (cross-paper): Which source datasets appear both in EIAD's DDQA-set and in the CGRO training set?  
  gold 2503.14162v2.pdf p3; 2603.27179v1.pdf p6; first gold hit: vector rank 4, rerank rank 4  
  rerank list: 2503.14162v2.pdf p4 (body), 2603.27179v1.pdf p13 (body), 2603.27179v1.pdf p15 (body), 2503.14162v2.pdf p3 (body), 2503.14162v2.pdf p3 (body)
- **cross-08** (cross-paper): Do Reason-IAD and the ReAL paper have the model write out its reasoning as text?  
  gold 2602.09850v2.pdf p2,5; 2603.27179v1.pdf p2,3,4; first gold hit: vector rank 3, rerank rank 3  
  rerank list: 2602.09850v2.pdf p4 (body), 2602.09850v2.pdf p4 (body), 2602.09850v2.pdf p2 (body), 2602.09850v2.pdf p5 (body), 2602.09850v2.pdf p15 (body)
- **reasoniad-07** (results): In the zero-shot MMAD setting, what average accuracy does Reason-IAD reach with Qwen3-VL-8B?  
  gold 2602.09850v2.pdf p7; first gold hit: vector rank 2, rerank rank 2  
  rerank list: 2602.09850v2.pdf p8 (body), 2602.09850v2.pdf p7 (body), 2602.09850v2.pdf p7 (body), 2602.09850v2.pdf p17 (body), 2602.09850v2.pdf p8 (body)

## Latency per stage (ms, from this machine; not production's network)

| Stage | median | p95 | max |
|---|---|---|---|
| query_embed | 506 | 1862 | 2229 |
| vector_search | 10 | 14 | 16 |

Voyage and rerank latencies are the first (uncached) call's, stored in the cache.

Voyage calls this run: {'embed_cached': 38}
