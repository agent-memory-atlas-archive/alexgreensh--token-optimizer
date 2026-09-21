# Compaction timing evaluation

Corpus: **synthetic challenge corpus**. Sessions: **30**. Checkpoints: **120**.

> Limitation: Synthetic challenge cases test policy behavior and evaluator integrity; they do not estimate production prevalence. Run this evaluator on private, session-grouped real data before changing runtime policy.

## Overall advisory results

| Policy | Recommendations | Boundary precision | Boundary recall | Product-truth precision |
|---|---:|---:|---:|---:|
| current_advisory | 90 | 51.1% (46/90) | 79.3% (46/58) | 47.8% (43/90) |
| semantic_boundary | 37 | 100.0% (37/37) | 63.8% (37/58) | 91.9% (34/37) |
| hybrid | 31 | 100.0% (31/31) | 53.4% (31/58) | 90.3% (28/31) |

## By occupancy band

| Band | Policy | Precision | Recall | Product truth |
|---|---|---:|---:|---:|
| <50 | current_advisory | n/a | 0.0% (0/12) | n/a |
| <50 | semantic_boundary | 100.0% (6/6) | 50.0% (6/12) | 100.0% (6/6) |
| <50 | hybrid | n/a | 0.0% (0/12) | n/a |
| 50-69 | current_advisory | 20.0% (6/30) | 100.0% (6/6) | 30.0% (9/30) |
| 50-69 | semantic_boundary | 100.0% (6/6) | 100.0% (6/6) | 100.0% (6/6) |
| 50-69 | hybrid | 100.0% (6/6) | 100.0% (6/6) | 100.0% (6/6) |
| 70-79 | current_advisory | 60.0% (18/30) | 100.0% (18/18) | 60.0% (18/30) |
| 70-79 | semantic_boundary | 100.0% (6/6) | 33.3% (6/18) | 100.0% (6/6) |
| 70-79 | hybrid | 100.0% (6/6) | 33.3% (6/18) | 100.0% (6/6) |
| 80+ | current_advisory | 73.3% (22/30) | 100.0% (22/22) | 53.3% (16/30) |
| 80+ | semantic_boundary | 100.0% (19/19) | 86.4% (19/22) | 84.2% (16/19) |
| 80+ | hybrid | 100.0% (19/19) | 86.4% (19/22) | 84.2% (16/19) |

## Continuity protection

Among 102 risky checkpoints, recent checkpoint coverage was 87.2% (89/102), cold-resume readiness was 99.0% (101/102), and joint protection was 86.3% (88/102).

## Interpretation

`current_advisory` is an observed decision for real data. On this synthetic corpus only, it is the canonical-Python-default approximation and not cross-host runtime truth. `semantic_boundary` is a challenger. `hybrid` requires both. Progressive checkpoint coverage is reported separately because protection is not a recommendation to compact.
