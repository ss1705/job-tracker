# Evaluation

Measured on **120 real emails** the sync search matched and **179** it did not, hand-labeled by the mailbox owner. Model: `openai/gpt-oss-120b`. Intervals are 95% Wilson confidence intervals; with samples this small, read the interval, not just the point estimate.

## Is it an application email? (detection)

| | Precision | Recall | F1 |
|---|---|---|---|
| Rules + LLM | 90% | 98% | 94% |
| Rules only | 95% | 83% | 88% |

## What does it extract? (over real application emails; a missed email counts as wrong)

| | Rules + LLM | Rules only |
|---|---|---|
| Status | 93% (43/46, 95% CI 82–98%) | 65% (30/46, 95% CI 51–77%) |
| Company | 96% (44/46, 95% CI 86–99%) | 72% (33/46, 95% CI 57–83%) |
| Role | 93% (43/46, 95% CI 82–98%) | 35% (16/46, 95% CI 23–49%) |

Status accuracy once an email is caught: 96% (43/45, 95% CI 85–99%).

## Status by class (rules + LLM)

| Status | Precision | Recall | F1 | Emails |
|---|---|---|---|---|
| Applied | 96% | 96% | 96% | 25 |
| OA | 100% | 100% | 100% | 2 |
| Interview | n/a | 0% | n/a | 1 |
| Rejected | 100% | 100% | 100% | 15 |
| Action | 100% | 67% | 80% | 3 |
| Other | 0% | n/a | n/a | 0 |

Confusion matrix (rows = truth, columns = prediction; *Missed* = not detected):

| truth \ pred | Applied | OA | Interview | Offer | Rejected | Action | Other | Missed |
|---|---|---|---|---|---|---|---|---|
| Applied | 24 | 0 | 0 | 0 | 0 | 0 | 1 | 0 |
| OA | 0 | 2 | 0 | 0 | 0 | 0 | 0 | 0 |
| Interview | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| Rejected | 0 | 0 | 0 | 0 | 15 | 0 | 0 | 0 |
| Action | 0 | 0 | 0 | 0 | 0 | 2 | 0 | 1 |

## Does the Gmail search find them? (search recall)

I read the subject and sender of **all 2297** emails the search did not match and flagged **120** as plausible application mail (recruiting-system senders or job words in the subject). Of those, 120 were labeled by hand and **5 were real application emails** (every suspect was checked, so this count is exact).

The other 2177 look unrelated; a random sample of 59 of them contained 0 real application emails (0% (0/59, 95% CI 0–6%)).

Estimate: about **129 found vs 5 missed**, search recall **96%** (95% range **42% to 97%**; at most about 138 missed).

## In real use

- 99 applications were created automatically from 390 emails processed (196 ignored as not application mail).
- The owner corrected the status by hand on 10 of them (10%); 0 are waiting in the review queue.
