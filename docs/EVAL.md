# Evaluation

> **These are the author's results, on the author's mailbox.** Accuracy depends on your own mail (employers, recruiting systems, language), so don't treat these figures as a guarantee. To measure yours, set `ENABLE_EVAL=1` in `.env`, open the **Evaluate** tab, sample, label and compute metrics. The parser was tuned on part of this sample, so the figures are optimistic until re-measured on a fresh held-out sample.

Measured on **120 real emails** the sync search matched and **179** it did not, hand-labeled by the mailbox owner. Model: `openai/gpt-oss-120b`. Intervals are 95% Wilson confidence intervals; with samples this small, read the interval, not just the point estimate.

## Is it an application email? (detection)

| | Precision | Recall | F1 |
|---|---|---|---|
| Rules + LLM | 90% | 100% | 95% |
| Rules only | 95% | 83% | 88% |

## What does it extract? (over real application emails; a missed email counts as wrong)

| | Rules + LLM | Rules only |
|---|---|---|
| Status | 98% (45/46, 95% CI 89–100%) | 65% (30/46, 95% CI 51–77%) |
| Company | 98% (45/46, 95% CI 89–100%) | 72% (33/46, 95% CI 57–83%) |
| Role | 96% (44/46, 95% CI 86–99%) | 35% (16/46, 95% CI 23–49%) |

Status accuracy once an email is caught: 98% (45/46, 95% CI 89–100%).

## Status by class (rules + LLM)

| Status | Precision | Recall | F1 | Emails |
|---|---|---|---|---|
| Applied | 96% | 100% | 98% | 25 |
| OA | 100% | 100% | 100% | 2 |
| Interview | n/a | 0% | n/a | 1 |
| Rejected | 100% | 100% | 100% | 15 |
| Action | 100% | 100% | 100% | 3 |

Confusion matrix (rows = truth, columns = prediction; *Missed* = not detected):

| truth \ pred | Applied | OA | Interview | Offer | Rejected | Action | Other | Missed |
|---|---|---|---|---|---|---|---|---|
| Applied | 25 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| OA | 0 | 2 | 0 | 0 | 0 | 0 | 0 | 0 |
| Interview | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| Rejected | 0 | 0 | 0 | 0 | 15 | 0 | 0 | 0 |
| Action | 0 | 0 | 0 | 0 | 0 | 3 | 0 | 0 |

## Does the Gmail search find them? (search recall)

*These figures describe search version 1. The search has since been widened (version 2: recruiting-system senders in every Gmail tab and extra wording) and now matches the real emails listed as missed below. A fresh sample is needed to measure the current search.*

I read the subject and sender of **all 2297** emails the search did not match and flagged **120** as plausible application mail (recruiting-system senders or job words in the subject). Of those, 119 were labeled by hand and **4 were real application emails** (the rest are estimated).

The other 2177 look unrelated; a random sample of 59 of them contained 0 real application emails (0% (0/59, 95% CI 0–6%)).

Estimate: about **129 found vs 4 missed**, search recall **97%** (95% range **41% to 99%**; at most about 143 missed).

## In real use

- 99 applications were created automatically from 390 emails processed (196 ignored as not application mail).
- The owner corrected the status by hand on 10 of them (10%); 0 are waiting in the review queue.
