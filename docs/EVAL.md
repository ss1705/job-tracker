# Evaluation

> **Author's results, on the author's mailbox.** Part 1 is a *held-out* test: emails the parser was never tuned on, scored against a parser that was frozen before they were labeled. Part 2 is the earlier in-sample result the parser was tuned on, kept for comparison only. Protocol: `docs/EVAL_PROTOCOL.md`.

Frozen at commit `fd15698` on 2026-10-22; held-out window starts 2026-10-11. The frozen code still matches the recorded hashes. 397 of 403 held-out labels were made blind (the parser's guess was hidden).

## Part 1: held-out results

Detection and extraction are scored on the **234 labeled emails the search matched** (203 emails were labeled after the freeze in total, 200 older ones). The suspects and random unmatched emails feed the search-recall estimate below. Intervals are 95% Wilson intervals; with this few real application emails they are wide.

### Is it an application email? (detection)

| | Precision | Recall | F1 |
|---|---|---|---|
| Rules + LLM | 88% | 98% | 93% |
| Rules only | 87% | 80% | 84% |

### What does it extract? (over real application emails; a missed email counts as wrong)

| | Rules + LLM | Rules only |
|---|---|---|
| Status | 85% (81/95, 95% CI 77–91%) | 55% (52/95, 95% CI 45–64%) |
| Company | 97% (92/95, 95% CI 91–99%) | 72% (68/95, 95% CI 62–80%) |
| Role | 62% (59/95, 95% CI 52–71%) | 9% (9/95, 95% CI 5–17%) |

Status accuracy once an email is caught: 87% (81/93, 95% CI 79–92%).

### Status by class (rules + LLM)

| Status | Precision | Recall (95% CI) | F1 | Emails |
|---|---|---|---|---|
| Applied | 95% | 90% (79–95%) | 92% | 58 |
| OA | 100% | 79% (52–92%) | 88% | 14 |
| Interview | 50% | 50% (10–90%) | 50% | 2 |
| Rejected | 100% | 81% (60–92%) | 89% | 21 |
| Action | 0% | n/a | n/a | 0 |
| Other | 0% | n/a | n/a | 0 |

Macro-averaged F1 over the 4 classes that have examples: **80%**. Classes with only a few examples have very wide intervals; read them as anecdotes, not measurements.

Confusion matrix (rows = truth, columns = prediction; *Missed* = not detected):

| truth \ pred | Applied | OA | Interview | Offer | Rejected | Action | Other | Missed |
|---|---|---|---|---|---|---|---|---|
| Applied | 52 | 0 | 0 | 0 | 0 | 4 | 1 | 1 |
| OA | 0 | 11 | 0 | 0 | 0 | 1 | 1 | 1 |
| Interview | 0 | 0 | 1 | 0 | 0 | 1 | 0 | 0 |
| Rejected | 3 | 0 | 1 | 0 | 17 | 0 | 0 | 0 |

Received after the freeze: 34 emails, status 75% (9/12, 95% CI 47–91%), company 100% (12/12, 95% CI 76–100%), role 67% (8/12, 95% CI 39–86%).

Older emails: 200 emails, status 87% (72/83, 95% CI 78–92%), company 96% (80/83, 95% CI 90–99%), role 61% (51/83, 95% CI 51–71%).

### Does the Gmail search find them? (search recall, emails after the freeze)

I read the subject and sender of **all 183** emails the search did not match received after the freeze and flagged **16** as plausible application mail. Of those, 14 were labeled by hand and **1 was a real application email** (the rest are estimated).

The other 167 look unrelated; a random sample of 155 of them contained 1 real application email (1% (1/155, 95% CI 0–4%)).

Estimate: about **14 found vs 2 missed**, search recall **86%** (95% range **43% to 98%**; at most about 11 missed).


### In real use since the freeze

- 5 applications were created from email; the owner corrected the status by hand on 1 (20%).


---

# Part 2: in-sample results (the parser was tuned on these)

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

| Status | Precision | Recall (95% CI) | F1 | Emails |
|---|---|---|---|---|
| Applied | 96% | 100% (87–100%) | 98% | 25 |
| OA | 100% | 100% (34–100%) | 100% | 2 |
| Interview | n/a | 0% (0–79%) | n/a | 1 |
| Rejected | 100% | 100% (80–100%) | 100% | 15 |
| Action | 100% | 100% (44–100%) | 100% | 3 |

Macro-averaged F1 over the 5 classes that have examples: **80%**. Classes with only a few examples have very wide intervals; read them as anecdotes, not measurements.

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

I read the subject and sender of **all 2297** emails the search did not match and flagged **120** as plausible application mail. Of those, 119 were labeled by hand and **4 were real application emails** (the rest are estimated).

The other 2177 look unrelated; a random sample of 59 of them contained 0 real application emails (0% (0/59, 95% CI 0–6%)).

Estimate: about **129 found vs 4 missed**, search recall **97%** (95% range **41% to 99%**; at most about 143 missed).

## In real use

- 102 applications were created automatically from 437 emails processed (221 ignored as not application mail).
- The owner corrected the status by hand on 13 of them (13%); 0 are waiting in the review queue.
