# Retrieval variants compared: test set

R@k and MRR@5 are on the ranked list the context is taken from (the rerank stage; for `rerank-off` the vector top 5). Context hit = a gold page is among the passages handed to the model. All metrics over answerable items, page level (`cross-09` at paper level). Each run's own file has the vector-stage numbers and page classes.

| Run | R@1 | R@3 | R@5 | MRR@5 | Context hit | Cross-paper gold-paper share in context | Reference share in context | Rerank requests (new Voyage calls) | New Voyage embed calls | Wall time |
|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 58% | 75% | 92% | 0.683 | 62% | 54% | 1% | 26 (26) | 26 | 1078 s |
| rerank-off | 71% | 88% | 92% | 0.795 | 88% | 54% | 0% | 0 (0) | 0 | 10 s |
| no-refs-ctx6000-rerank-off | 75% | 92% | 96% | 0.837 | 96% | 67% | 0% | 0 (0) | 0 | 5 s |
| no-refs-top20-rerank10-ctx6000 | 75% | 100% | 100% | 0.854 | 100% | 58% | 0% | 26 (26) | 0 | 925 s |

## Per type

| Type | Run | n | R@1 | R@3 | R@5 | MRR@5 | Context hit |
|---|---|---|---|---|---|---|---|
| cross-paper | baseline | 4 | 50% | 100% | 100% | 0.667 | 75% |
| cross-paper | rerank-off | 4 | 75% | 100% | 100% | 0.875 | 100% |
| cross-paper | no-refs-ctx6000-rerank-off | 4 | 75% | 100% | 100% | 0.875 | 100% |
| cross-paper | no-refs-top20-rerank10-ctx6000 | 4 | 50% | 100% | 100% | 0.708 | 100% |
| factual | baseline | 6 | 50% | 50% | 83% | 0.567 | 50% |
| factual | rerank-off | 6 | 50% | 67% | 83% | 0.625 | 83% |
| factual | no-refs-ctx6000-rerank-off | 6 | 50% | 67% | 83% | 0.625 | 83% |
| factual | no-refs-top20-rerank10-ctx6000 | 6 | 83% | 100% | 100% | 0.889 | 100% |
| limitation | baseline | 4 | 75% | 75% | 75% | 0.750 | 75% |
| limitation | rerank-off | 4 | 50% | 75% | 75% | 0.583 | 75% |
| limitation | no-refs-ctx6000-rerank-off | 4 | 75% | 100% | 100% | 0.833 | 100% |
| limitation | no-refs-top20-rerank10-ctx6000 | 4 | 75% | 100% | 100% | 0.875 | 100% |
| method | baseline | 6 | 33% | 67% | 100% | 0.556 | 33% |
| method | rerank-off | 6 | 100% | 100% | 100% | 1.000 | 100% |
| method | no-refs-ctx6000-rerank-off | 6 | 100% | 100% | 100% | 1.000 | 100% |
| method | no-refs-top20-rerank10-ctx6000 | 6 | 83% | 100% | 100% | 0.889 | 100% |
| results | baseline | 4 | 100% | 100% | 100% | 1.000 | 100% |
| results | rerank-off | 4 | 75% | 100% | 100% | 0.875 | 75% |
| results | no-refs-ctx6000-rerank-off | 4 | 75% | 100% | 100% | 0.875 | 100% |
| results | no-refs-top20-rerank10-ctx6000 | 4 | 75% | 100% | 100% | 0.875 | 100% |

## Cross-paper items: share of gold papers in the context

| id | baseline | rerank-off | no-refs-ctx6000-rerank-off | no-refs-top20-rerank10-ctx6000 |
|---|---|---|---|---|
| cross-01 | 50% | 50% | 50% | 50% |
| cross-03 | 50% | 50% | 100% | 100% |
| cross-06 | 50% | 50% | 50% | 50% |
| cross-07 | 67% | 67% | 67% | 33% |

## Flips versus baseline

| Run | Context: miss → hit | Context: hit → miss | R@1: miss → hit | R@1: hit → miss |
|---|---|---|---|---|
| rerank-off | eiad-06, reasoniad-01, real-05, vlmaoi-01, vlmaoi-05, lmv-06, cross-03 | real-08 | eiad-06, real-05, vlmaoi-05, lmv-06, cross-03, cross-07 | real-08, real-09, cross-01 |
| no-refs-ctx6000-rerank-off | eiad-06, eiad-09, reasoniad-01, real-05, vlmaoi-01, vlmaoi-05, lmv-06, cross-03 | none | eiad-06, eiad-09, real-05, vlmaoi-05, lmv-06, cross-03, cross-07 | real-08, real-09, cross-01 |
| no-refs-top20-rerank10-ctx6000 | eiad-06, eiad-09, reasoniad-01, real-02, real-05, vlmaoi-01, vlmaoi-05, lmv-06, cross-03 | none | eiad-06, eiad-09, real-02, real-05, vlmaoi-01, lmv-06, cross-03 | real-08, real-09, cross-01 |
