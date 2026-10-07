"""Score the parser against tests/labeled_emails.csv.

    .venv/bin/python evaluate.py          # rules + LLM (if GROQ_API_KEY set)
    .venv/bin/python evaluate.py --rules  # rules only
"""
import csv
import sys
from collections import Counter

from jobtracker.db import company_key
from jobtracker.parser import llm_available, parse_email

use_llm = "--rules" not in sys.argv
rows = [r for r in csv.DictReader(open("tests/labeled_emails.csv", encoding="utf-8")) if r["true_is_job_related"] != ""]

job_ok = status_ok = company_ok = n_job = 0
confusion = Counter()
for r in rows:
    p = parse_email(r["subject"], r["from"], r["body_preview"] or "", use_llm=use_llm)
    truth_job = r["true_is_job_related"].strip().lower() == "true"
    pred_job = p.is_job and bool(p.company)
    job_ok += truth_job == pred_job
    if truth_job:
        n_job += 1
        ts, ps = r["true_status"] or "None", (p.status if pred_job else None) or "None"
        status_ok += ts == ps
        confusion[(ts, ps)] += 1
        company_ok += company_key(r["true_company"]) == company_key(p.company)
        if "-v" in sys.argv and (ts != ps or company_key(r["true_company"]) != company_key(p.company)):
            print(f"  ✗ {r['subject'][:55]!r}: {ts}→{ps} | {r['true_company']!r}→{p.company!r}")

print(f"mode: {'rules+LLM' if use_llm and llm_available() else 'rules only'}   n={len(rows)} ({n_job} job-related)")
print(f"job-related detection: {job_ok}/{len(rows)} = {job_ok/len(rows):.0%}")
print(f"status (job rows):     {status_ok}/{n_job} = {status_ok/n_job:.0%}")
print(f"company (job rows):    {company_ok}/{n_job} = {company_ok/n_job:.0%}")
print("confusion (true→pred):", dict(confusion))
