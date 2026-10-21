"""Metrics for the email-parsing pipeline. Pure functions over labeled items, so they are easy to test.

An item is {truth: {is_job, company, role, status}, pred: {is_job, company, role, status}}.
Status labels: Applied / OA / Interview / Offer / Rejected / Action (they need something from you) / Other ("Other" = job-related but no clear outcome).
"""
import math
from collections import Counter, defaultdict

from . import db

STATUS_LABELS = ["Applied", "OA", "Interview", "Offer", "Rejected", "Action", "Other"]


def norm_status(s: str | None) -> str:
    return s if s in STATUS_LABELS else "Other"


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% confidence interval for a proportion (honest about small samples)."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def _rate(k: int, n: int) -> dict:
    lo, hi = wilson(k, n)
    return {"k": k, "n": n, "value": (k / n) if n else None, "ci": [round(lo, 3), round(hi, 3)]}


def _prf(tp: int, fp: int, fn: int) -> dict:
    p = tp / (tp + fp) if tp + fp else None
    r = tp / (tp + fn) if tp + fn else None
    f = 2 * p * r / (p + r) if p and r else (0.0 if (p is not None and r is not None) else None)
    return {"precision": p, "recall": r, "f1": f, "tp": tp, "fp": fp, "fn": fn}


def company_match(truth: str, pred: str) -> bool:
    return bool(truth and pred) and db.same_company(truth, pred)


def role_match(truth: str, pred: str) -> bool:
    if not truth and not pred:
        return True
    return bool(truth and pred) and db.role_similarity(truth, pred) >= 0.8


def detection(items: list[dict]) -> dict:
    """Is-this-a-job-email: precision / recall / F1 where positive = a real application-related email."""
    tp = sum(1 for i in items if i["truth"]["is_job"] and i["pred"]["is_job"])
    fp = sum(1 for i in items if not i["truth"]["is_job"] and i["pred"]["is_job"])
    fn = sum(1 for i in items if i["truth"]["is_job"] and not i["pred"]["is_job"])
    tn = len(items) - tp - fp - fn
    out = _prf(tp, fp, fn)
    out.update(tn=tn, n=len(items), accuracy=_rate(tp + tn, len(items)))
    return out


def extraction(items: list[dict]) -> dict:
    """Status / company / role accuracy over emails that really are job-related. A missed email counts as wrong."""
    jobs = [i for i in items if i["truth"]["is_job"]]
    caught = [i for i in jobs if i["pred"]["is_job"]]
    status_ok = [i for i in jobs if i["pred"]["is_job"] and norm_status(i["pred"].get("status")) == norm_status(i["truth"].get("status"))]
    company_ok = [i for i in jobs if i["pred"]["is_job"] and company_match(i["truth"].get("company", ""), i["pred"].get("company", ""))]
    role_ok = [i for i in jobs if i["pred"]["is_job"] and role_match(i["truth"].get("role", ""), i["pred"].get("role", ""))]
    # status given the email was caught (separates "missed it" from "mislabeled it")
    status_given_caught = [i for i in caught if norm_status(i["pred"].get("status")) == norm_status(i["truth"].get("status"))]
    return {"n_job_emails": len(jobs), "status": _rate(len(status_ok), len(jobs)), "status_given_caught": _rate(len(status_given_caught), len(caught)),
            "company": _rate(len(company_ok), len(jobs)), "role": _rate(len(role_ok), len(jobs))}


def status_breakdown(items: list[dict]) -> dict:
    """Per-class precision / recall / F1 and a confusion matrix (rows = truth, columns = prediction; 'Missed' = not detected)."""
    jobs = [i for i in items if i["truth"]["is_job"]]
    pairs = [(norm_status(i["truth"].get("status")), norm_status(i["pred"].get("status")) if i["pred"]["is_job"] else "Missed") for i in jobs]
    matrix = defaultdict(Counter)
    for t, p in pairs:
        matrix[t][p] += 1
    per_class = {}
    for c in STATUS_LABELS:
        tp = matrix[c][c]
        fp = sum(matrix[t][c] for t in STATUS_LABELS if t != c)
        fn = sum(n for p, n in matrix[c].items() if p != c)
        support = sum(matrix[c].values())
        per_class[c] = {**_prf(tp, fp, fn), "support": support,
                        "recall_ci": [round(x, 3) for x in wilson(tp, tp + fn)] if tp + fn else None,
                        "precision_ci": [round(x, 3) for x in wilson(tp, tp + fp)] if tp + fp else None}
    scored = [v["f1"] for v in per_class.values() if v["support"] and v["f1"] is not None]
    present = [v for v in per_class.values() if v["support"]]
    macro = (sum((v["f1"] or 0.0) for v in present) / len(present)) if present else None   # classes with no true examples don't count
    return {"macro_f1": macro, "classes_with_examples": len(present), "per_class": per_class, "confusion": {t: dict(matrix[t]) for t in STATUS_LABELS if matrix[t]}, "columns": STATUS_LABELS + ["Missed"]}


def search_recall(matched_items: list[dict], unmatched_items: list[dict], n_matched_universe: int, n_unmatched_universe: int) -> dict:
    """How many real application emails does the Gmail search find? Estimated by labeling a random sample of each side:
    recall = (job emails among matched) / (job emails among matched + job emails hiding in the unmatched mail)."""
    pm = sum(1 for i in matched_items if i["truth"]["is_job"]) / len(matched_items) if matched_items else 0
    pu = sum(1 for i in unmatched_items if i["truth"]["is_job"]) / len(unmatched_items) if unmatched_items else 0
    found, hidden = pm * n_matched_universe, pu * n_unmatched_universe
    missed_sample = sum(1 for i in unmatched_items if i["truth"]["is_job"])
    # honest interval: pair the pessimistic rate on each side (few found, many hiding) and the optimistic one
    pm_lo, pm_hi = wilson(sum(1 for i in matched_items if i["truth"]["is_job"]), len(matched_items))
    pu_lo, pu_hi = wilson(missed_sample, len(unmatched_items))
    ratio = lambda f, h: (f / (f + h)) if (f + h) else None
    lo = ratio(pm_lo * n_matched_universe, pu_hi * n_unmatched_universe)
    hi = ratio(pm_hi * n_matched_universe, pu_lo * n_unmatched_universe)
    return {"estimated_found": round(found), "estimated_missed": round(hidden), "missed_upper_bound": round(pu_hi * n_unmatched_universe),
            "recall": (found / (found + hidden)) if (found + hidden) else None,
            "recall_ci": [round(lo, 3), round(hi, 3)] if lo is not None and hi is not None else None,
            "unmatched_sample": _rate(missed_sample, len(unmatched_items)), "matched_universe": n_matched_universe,
            "unmatched_universe": n_unmatched_universe}


def full_report(matched: list[dict], unmatched: list[dict], n_matched_universe: int, n_unmatched_universe: int) -> dict:
    return {"n_matched_labeled": len(matched), "n_unmatched_labeled": len(unmatched),
            "detection": detection(matched), "extraction": extraction(matched), "status": status_breakdown(matched),
            "search": search_recall(matched, unmatched, n_matched_universe, n_unmatched_universe)}


def search_recall_stratified(matched_items: list[dict], cold_items: list[dict], hot_items: list[dict], n_matched_universe: int,
                             n_hot_universe: int, n_cold_universe: int) -> dict:
    """Search recall using two strata of the unmatched mail:
      hot  = looks job-related from sender/subject alone (recruiting-system senders, job words): every one is meant to be labeled,
             so its count of real application emails is exact when fully labeled (otherwise a sample estimate with an interval);
      cold = everything else: a random sample, which gives a rate with a confidence interval.
    recall = found / (found + hidden), with an honest interval from the pessimistic and optimistic pairings."""
    pm_k = sum(1 for i in matched_items if i["truth"]["is_job"])
    pm_lo, pm_hi = wilson(pm_k, len(matched_items))
    pm = pm_k / len(matched_items) if matched_items else 0
    found, found_lo, found_hi = pm * n_matched_universe, pm_lo * n_matched_universe, pm_hi * n_matched_universe

    hk = sum(1 for i in hot_items if i["truth"]["is_job"])
    if n_hot_universe and len(hot_items) >= n_hot_universe:                  # every suspect labeled: exact
        hot_est = hot_lo = hot_hi = float(hk)
    elif hot_items:
        lo, hi = wilson(hk, len(hot_items))
        hot_est, hot_lo, hot_hi = hk / len(hot_items) * n_hot_universe, lo * n_hot_universe, hi * n_hot_universe
    else:
        hot_est = hot_lo = hot_hi = 0.0
    ck = sum(1 for i in cold_items if i["truth"]["is_job"])
    clo, chi = wilson(ck, len(cold_items))
    cold_est = (ck / len(cold_items) * n_cold_universe) if cold_items else 0.0
    cold_lo, cold_hi = clo * n_cold_universe, chi * n_cold_universe

    ratio = lambda f, h: (f / (f + h)) if (f + h) else None
    return {"estimated_found": round(found), "estimated_missed": round(hot_est + cold_est), "missed_upper_bound": round(hot_hi + cold_hi),
            "recall": ratio(found, hot_est + cold_est), "recall_ci": [round(ratio(found_lo, hot_hi + cold_hi) or 0, 3), round(ratio(found_hi, hot_lo + cold_lo) or 1, 3)],
            "hot": {"universe": n_hot_universe, "labeled": len(hot_items), "job": hk, "exact": bool(n_hot_universe and len(hot_items) >= n_hot_universe)},
            "cold": {"universe": n_cold_universe, "labeled": len(cold_items), "job": ck, "rate": _rate(ck, len(cold_items))},
            "unmatched_sample": _rate(ck, len(cold_items)), "matched_universe": n_matched_universe, "unmatched_universe": n_hot_universe + n_cold_universe,
            "stratified": True}
