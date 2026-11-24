# Retrieval eval: baseline-test

Run 2026-11-24 17:58 UTC on `eval/questions_test.jsonl` (26 questions: 24 answerable, 2 unanswerable). Collection `researchmind_eval`. Wall time 1078 s, of which 1040 s was free-tier pacing.

Variant `baseline`: Production retrieval, reranking on.

## Settings measured

- Pages: PyMuPDF physical pages from 1, clean_text per page, pages under 100 chars skipped; RecursiveCharacterTextSplitter 1500 chars, 300 overlap; payload paper = file name.
- Embeddings: Voyage voyage-4, input_type document for chunks and query for the question; Qdrant 1024-d cosine.
- Search: query_points limit 8, owner_id filter; keep_live_hits skipped (would keep every hit here).
- Rerank (RERANK_ENABLED=true): vector top 5 only, Voyage rerank-3 on each chunk's first 400 chars. With the default RERANK_ENABLED=false production keeps the vector top 5 unchanged.
- Vector search: Qdrant embedded local mode (exact search). At this collection size Qdrant Cloud generally searches exactly too, so rankings should match; not verified against Cloud.
- Context: top 4 after rerank, _select_evidence within 4000 chars.

## Metrics (answerable items, page level)

Hit = a retrieved chunk is on any gold page of any gold paper (`cross-09`: any page of a gold paper). k counts chunks, as production does. `context` = passages handed to the model. The rerank stage has only 5 items (production cuts to the vector top 5 before reranking), so its recall@8 is n/a and its recall@5 equals the vector stage's.

| Stage | Type | n | R@1 | R@3 | R@5 | R@8 | MRR@5 | MRR | paper-hit@5 |
|---|---|---|---|---|---|---|---|---|---|
| vector | all | 24 | 71% | 88% | 92% | 96% | 0.795 | 0.801 | 100% |
| vector | cross-paper | 4 | 75% | 100% | 100% | 100% | 0.875 | 0.875 | 100% |
| vector | factual | 6 | 50% | 67% | 83% | 100% | 0.625 | 0.649 | 100% |
| vector | limitation | 4 | 50% | 75% | 75% | 75% | 0.583 | 0.583 | 100% |
| vector | method | 6 | 100% | 100% | 100% | 100% | 1.000 | 1.000 | 100% |
| vector | results | 4 | 75% | 100% | 100% | 100% | 0.875 | 0.875 | 100% |
| rerank | all | 24 | 58% | 75% | 92% | n/a | 0.683 | 0.683 | 100% |
| rerank | cross-paper | 4 | 50% | 100% | 100% | n/a | 0.667 | 0.667 | 100% |
| rerank | factual | 6 | 50% | 50% | 83% | n/a | 0.567 | 0.567 | 100% |
| rerank | limitation | 4 | 75% | 75% | 75% | n/a | 0.750 | 0.750 | 100% |
| rerank | method | 6 | 33% | 67% | 100% | n/a | 0.556 | 0.556 | 100% |
| rerank | results | 4 | 100% | 100% | 100% | n/a | 1.000 | 1.000 | 100% |

Context stage: a gold page reaches the model for 62% of answerable items (mean 2.5 passages).

## Cross-paper items

| id | gold papers | found in vector top 8 | found in rerank list | found in context |
|---|---|---|---|---|
| cross-01 | 2 | 50% | 50% | 50% |
| cross-03 | 2 | 100% | 100% | 50% |
| cross-06 | 2 | 100% | 50% | 50% |
| cross-07 | 3 | 67% | 67% | 67% |

## Unanswerable items: top rerank score

| id | top rerank score | top vector score | top page (class) |
|---|---|---|---|
| unans-04 | 0.353 | 0.538 | 2603.27179v1.pdf p14 (body) |
| unans-05 | 0.362 | 0.385 | 2603.27179v1.pdf p15 (body) |

Answerable items' top rerank score: min 0.382, p25 0.508, median 0.634, p75 0.752, max 0.908 (n=24). Answerable items whose top rerank score is below the highest unanswerable top score: 0 of 24.

## Retrieved page classes (share of retrieved chunks, all questions)

| Stage | body | mixed | reference |
|---|---|---|---|
| vector | 90% | 8% | 2% |
| rerank | 92% | 7% | 1% |
| context | 90% | 9% | 1% |

## Worst misses

Order: no gold page in the vector top 8 first, then later first-hit rank after rerank, then higher top rerank score (confidently wrong).

- **eiad-09** (limitation): What do the EIAD authors say still needs improving in their work?  
  gold 2503.14162v2.pdf p6; first gold hit: vector rank None, rerank rank None  
  rerank list: 2602.09850v2.pdf p18 (body), 2602.09850v2.pdf p22 (body), 2503.14162v2.pdf p1 (body), 2602.09850v2.pdf p22 (body), 2503.14162v2.pdf p1 (body)
- **real-02** (factual): How large is the training set used for CGRO, and what kind of labels does it carry?  
  gold 2603.27179v1.pdf p6; first gold hit: vector rank 7, rerank rank None  
  rerank list: ETASR_18859.pdf p3 (body), 2603.27179v1.pdf p15 (body), 2603.27179v1.pdf p13 (body), 2503.14162v2.pdf p4 (body), 2503.14162v2.pdf p3 (body)
- **reasoniad-01** (factual): Which encoder does Reason-IAD use to retrieve category knowledge, and how many categories does it retrieve per image?  
  gold 2602.09850v2.pdf p7,14; first gold hit: vector rank 4, rerank rank 5  
  rerank list: 2602.09850v2.pdf p15 (body), 2602.09850v2.pdf p5 (body), 2602.09850v2.pdf p4 (body), 2602.09850v2.pdf p4 (body), 2602.09850v2.pdf p7 (body)
- **vlmaoi-01** (factual): What dataset was VLM-AOI evaluated on, and how many defect classes does it provide?  
  gold ETASR_18859.pdf p3; first gold hit: vector rank 2, rerank rank 5  
  rerank list: ETASR_18859.pdf p4 (body), ETASR_18859.pdf p6 (mixed), ETASR_18859.pdf p4 (body), ETASR_18859.pdf p5 (body), ETASR_18859.pdf p3 (body)
- **lmv-06** (method): In Language Meets Vision, how are the image and text features combined to produce the segmentation map?  
  gold ssrn-5745634.pdf p8,9; first gold hit: vector rank 1, rerank rank 4  
  rerank list: 2503.14162v2.pdf p4 (body), ssrn-5745634.pdf p4 (body), ssrn-5745634.pdf p15 (body), ssrn-5745634.pdf p9 (body), ssrn-5745634.pdf p8 (body)

## Latency per stage (ms, from this machine; not production's network)

| Stage | median | p95 | max |
|---|---|---|---|
| query_embed | 326 | 1035 | 1299 |
| vector_search | 7 | 9 | 17 |
| rerank | 387 | 1136 | 1310 |

Voyage and rerank latencies are the first (uncached) call's, stored in the cache.

Voyage calls this run: {'embed_api': 26, 'embed_tokens': 466, 'rerank_api': 26, 'rerank_tokens': 18173}
