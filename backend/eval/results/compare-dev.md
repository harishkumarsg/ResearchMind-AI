# Retrieval variants compared: dev set

R@k and MRR@5 are on the ranked list the context is taken from (the rerank stage; for `rerank-off` the vector top 5). Context hit = a gold page is among the passages handed to the model. All metrics over answerable items, page level (`cross-09` at paper level). Each run's own file has the vector-stage numbers and page classes.

| Run | R@1 | R@3 | R@5 | MRR@5 | Context hit | Cross-paper gold-paper share in context | Reference share in context | Rerank requests (new Voyage calls) | New Voyage embed calls | Wall time |
|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 83% | 91% | 94% | 0.869 | 91% | 69% | 6% | 38 (38) | 58 | 2603 s |
| rerank-off | 83% | 86% | 94% | 0.863 | 89% | 69% | 6% | 0 (0) | 0 | 15 s |
| no-refs | 80% | 94% | 97% | 0.864 | 91% | 65% | 0% | 38 (8) | 4 | 240 s |
| no-refs-top20-rerank10 | 86% | 97% | 97% | 0.914 | 94% | 81% | 0% | 38 (38) | 0 | 1317 s |
| no-refs-top20-rerank10-ctx6000 | 86% | 97% | 97% | 0.914 | 97% | 81% | 0% | 38 (0) | 0 | 7 s |

## Per type

| Type | Run | n | R@1 | R@3 | R@5 | MRR@5 | Context hit |
|---|---|---|---|---|---|---|---|
| cross-paper | baseline | 5 | 60% | 80% | 80% | 0.700 | 80% |
| cross-paper | rerank-off | 5 | 40% | 40% | 80% | 0.490 | 40% |
| cross-paper | no-refs | 5 | 60% | 100% | 100% | 0.767 | 80% |
| cross-paper | no-refs-top20-rerank10 | 5 | 60% | 100% | 100% | 0.800 | 100% |
| cross-paper | no-refs-top20-rerank10-ctx6000 | 5 | 60% | 100% | 100% | 0.800 | 100% |
| factual | baseline | 9 | 100% | 100% | 100% | 1.000 | 100% |
| factual | rerank-off | 9 | 100% | 100% | 100% | 1.000 | 100% |
| factual | no-refs | 9 | 89% | 100% | 100% | 0.944 | 100% |
| factual | no-refs-top20-rerank10 | 9 | 89% | 100% | 100% | 0.944 | 89% |
| factual | no-refs-top20-rerank10-ctx6000 | 9 | 89% | 100% | 100% | 0.944 | 100% |
| limitation | baseline | 6 | 50% | 67% | 83% | 0.597 | 83% |
| limitation | rerank-off | 6 | 67% | 67% | 83% | 0.708 | 83% |
| limitation | no-refs | 6 | 50% | 67% | 83% | 0.597 | 83% |
| limitation | no-refs-top20-rerank10 | 6 | 83% | 83% | 83% | 0.833 | 83% |
| limitation | no-refs-top20-rerank10-ctx6000 | 6 | 83% | 83% | 83% | 0.833 | 83% |
| method | baseline | 9 | 100% | 100% | 100% | 1.000 | 100% |
| method | rerank-off | 9 | 100% | 100% | 100% | 1.000 | 100% |
| method | no-refs | 9 | 100% | 100% | 100% | 1.000 | 100% |
| method | no-refs-top20-rerank10 | 9 | 89% | 100% | 100% | 0.944 | 100% |
| method | no-refs-top20-rerank10-ctx6000 | 9 | 89% | 100% | 100% | 0.944 | 100% |
| results | baseline | 6 | 83% | 100% | 100% | 0.889 | 83% |
| results | rerank-off | 6 | 83% | 100% | 100% | 0.917 | 100% |
| results | no-refs | 6 | 83% | 100% | 100% | 0.889 | 83% |
| results | no-refs-top20-rerank10 | 6 | 100% | 100% | 100% | 1.000 | 100% |
| results | no-refs-top20-rerank10-ctx6000 | 6 | 100% | 100% | 100% | 1.000 | 100% |

## Cross-paper items: share of gold papers in the context

| id | baseline | rerank-off | no-refs | no-refs-top20-rerank10 | no-refs-top20-rerank10-ctx6000 |
|---|---|---|---|---|---|
| cross-02 | 100% | 100% | 100% | 100% | 100% |
| cross-04 | 33% | 33% | 33% | 67% | 67% |
| cross-05 | 100% | 100% | 100% | 100% | 100% |
| cross-08 | 50% | 50% | 50% | 100% | 100% |
| cross-09 | 60% | 60% | 40% | 40% | 40% |

## Flips versus baseline

| Run | Context: miss → hit | Context: hit → miss | R@1: miss → hit | R@1: hit → miss |
|---|---|---|---|---|
| rerank-off | reasoniad-07 | cross-05, cross-08 | lmv-10 | cross-05 |
| no-refs | none | none | none | lmv-01 |
| no-refs-top20-rerank10 | reasoniad-07, cross-04 | lmv-01 | reasoniad-07, reasoniad-10, lmv-10, cross-04 | vlmaoi-04, lmv-01, cross-05 |
| no-refs-top20-rerank10-ctx6000 | reasoniad-07, cross-04 | none | reasoniad-07, reasoniad-10, lmv-10, cross-04 | vlmaoi-04, lmv-01, cross-05 |
