# Held-out evaluation protocol

Written and committed **before** any held-out email was labeled, so the rules can't be bent to fit the results.

## Why

The first evaluation (`docs/EVAL.md`, part 2 of the final report) was measured on emails the parser was tuned on: errors found there were fixed, then re-scored on the same emails. Those numbers are optimistic. This protocol measures the same parser on emails it has never been adjusted for.

## What is frozen

| File | Why |
|---|---|
| `jobtracker/parser.py` | decides application / status / company / role |
| `jobtracker/gmail.py` | defines the search (`QUERY_VERSION` 2) that finds candidate emails |

The Evaluate tab records the SHA-256 of both files and the git commit when the freeze is made, and shows a warning (and refuses to sample) if either changes. The commit is also tagged `eval-freeze-20261021`.

`parser.py` has not changed since 2026-10-10 (commit `2fd72eb`). `gmail.py` changed once afterwards, on 2026-10-14, to handle an expired Gmail login (`RefreshError` becomes a visible "reconnect" state); the search query and what is matched are unchanged from 2026-10-10.

The language model (`openai/gpt-oss-120b` with fallbacks) is a hosted service and is not frozen; its version can drift. This is a limitation of the result.

## Held-out sets

Cutoff date: **2026-10-11**. Tuning used mail from before that date only.

1. **Prospective set:** every email received on or after the cutoff. All emails the search matched, all "suspect" unmatched emails (recruiting-system senders or job words in the subject), and a random sample of about 200 of the remaining unmatched emails. Nothing is cherry-picked: the matched and suspect groups are complete.
2. **Older set:** a random sample (about 200) of emails from before the cutoff that the search matches and that were not in the tuning sample.

Emails already in the tuning sample are excluded from both sets.

## Labeling

- Blind: the Evaluate tab hides the parser's is-it-an-application and status guess. Company and role stay pre-filled to correct.
- Label from the email itself. Labels are not revised after seeing results.
- Statuses: Applied, OA, Interview, Offer, Rejected, Action needed, Other.

## Rules

1. No change to the frozen files after the freeze.
2. Errors found are recorded as future work. They are not fixed and re-scored on the held-out sets.
3. Everything labeled is reported, whatever the result. Results may be lower than the in-sample numbers.
4. Per-class numbers over few examples are reported with their intervals and described as anecdotes, not measurements.

## What is reported

- Detection precision / recall / F1, status / company / role accuracy (95% Wilson intervals), per-class results, macro-F1 and a confusion matrix, for rules-only and rules + LLM.
- Search recall on the prospective window (exact over suspects, sampled over the rest, with an honest interval).
- End to end, with no extra labeling: applications created since the freeze and how many needed a manual status correction.
- In-sample (tuning) numbers beside them, labeled as such.

## Known limitations

- One person's mailbox; one job search; a small number of real application emails.
- A single labeler. Labels were made by the same person who built the tool.
- The labeler had seen the in-sample errors, which may bias blind labeling slightly.
