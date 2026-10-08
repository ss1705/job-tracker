"""Build and score an evaluation set from your own mailbox.

  sample()          pick random emails the sync query MATCHES and ones it does NOT (to measure what the search misses)
  predict_pending() run the real pipeline on them and store its prediction
  label()           you confirm or correct each one (the web UI's Evaluate tab)
  report()          metrics from the labeled items; rules-only vs full pipeline; plus in-the-wild numbers from the tracker

Everything is stored locally (data/, gitignored). Only aggregate numbers go into the committable report.
"""
import json
import random
import threading
from datetime import date, datetime, timedelta, timezone

from . import config, db, evalmetrics, gmail, llm
from .parser import clean_body, parse_email

MAX_BODY = 4000

state = {"running": False, "phase": "", "done": 0, "total": 0, "error": ""}
_lock = threading.Lock()


def _ids(svc, q: str) -> list[str]:
    ids, token = [], None
    while True:
        r = svc.users().messages().list(userId="me", q=q, maxResults=500, pageToken=token).execute()
        ids += [m["id"] for m in r.get("messages", [])]
        token = r.get("nextPageToken")
        if not token:
            return ids


def queries(since: date) -> tuple[str, str]:
    matched = gmail.build_query(since)
    base = f"after:{(since - timedelta(days=1)):%Y/%m/%d} -from:me -in:spam -in:trash"
    unmatched = f"{base} -({gmail.QUERY_TERMS} -category:promotions -category:social)"
    return matched, unmatched


def _predict(subject: str, sender: str, body: str, use_llm: bool, extra_blocked: tuple = ()) -> dict:
    p = parse_email(subject, sender, body, use_llm=use_llm, extra_blocked=extra_blocked)
    is_job = bool(p.is_job and p.company)            # exactly the sync's rule for "this becomes an application"
    return {"is_job": is_job, "company": p.company if is_job else "", "role": p.role if is_job else "",
            "status": p.status if is_job else None, "via": p.via}


def _blocked(conn) -> tuple:
    return tuple(r[0] for r in conn.execute("SELECT pattern FROM blocked_senders"))


# ---------------------------------------------------------------- sampling + predictions (background)

def start_sample(since: date, n_matched: int = 120, n_unmatched: int = 60, seed: int = 7) -> bool:
    if not _lock.acquire(blocking=False):
        return False
    state.update(running=True, phase="Searching Gmail…", done=0, total=0, error="")
    threading.Thread(target=_run_sample, args=(since, n_matched, n_unmatched, seed), daemon=True).start()
    return True


def _run_sample(since, n_matched, n_unmatched, seed):
    try:
        svc = gmail.service()
        qm, qu = queries(since)
        m_ids, u_ids = _ids(svc, qm), _ids(svc, qu)
        with db.session() as conn:
            have = {r[0] for r in conn.execute("SELECT gmail_id FROM eval_items")}
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('eval_universe', ?)", (json.dumps(
                {"matched": len(m_ids), "unmatched": len(u_ids), "since": since.isoformat(),
                 "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}),))
        rng = random.Random(seed)
        picks = [(i, "matched") for i in rng.sample([i for i in m_ids if i not in have], min(n_matched, len([i for i in m_ids if i not in have])))]
        picks += [(i, "unmatched") for i in rng.sample([i for i in u_ids if i not in have], min(n_unmatched, len([i for i in u_ids if i not in have])))]
        state.update(phase="Downloading emails…", total=len(picks))
        for gid, kind in picks:
            msg = gmail.fetch_message(svc, gid)
            with db.session() as conn:
                conn.execute("INSERT OR IGNORE INTO eval_items (gmail_id, kind, subject, sender, received_at, body) VALUES (?,?,?,?,?,?)",
                             (gid, kind, msg["subject"][:300], msg["sender"][:200], msg["received_at"], clean_body(msg["body"], MAX_BODY)))
            state["done"] += 1
        _predict_pending()
    except Exception as e:
        state["error"] = f"{type(e).__name__}: {e}"
    finally:
        state.update(running=False, phase="Done" if not state["error"] else state["phase"])
        _lock.release()


def start_predict() -> bool:
    if not _lock.acquire(blocking=False):
        return False
    state.update(running=True, phase="Running the pipeline…", done=0, total=0, error="")

    def run():
        try:
            _predict_pending()
        except Exception as e:
            state["error"] = f"{type(e).__name__}: {e}"
        finally:
            state.update(running=False, phase="Done" if not state["error"] else state["phase"])
            _lock.release()
    threading.Thread(target=run, daemon=True).start()
    return True


def _predict_pending():
    with db.session() as conn:
        rows = conn.execute("SELECT id, kind, subject, sender, body FROM eval_items WHERE pred_json='' ORDER BY id").fetchall()
        extra = _blocked(conn)
    state.update(phase="Running the pipeline…", total=len(rows), done=0)
    for r in rows:
        try:
            pred = _predict(r["subject"], r["sender"], r["body"], use_llm=(r["kind"] == "matched"), extra_blocked=extra)
        except llm.LLMExhausted as e:
            state["error"] = str(e)
            return                                   # resume later; already-predicted rows are kept
        with db.session() as conn:
            conn.execute("UPDATE eval_items SET pred_json=? WHERE id=?", (json.dumps(pred), r["id"]))
        state["done"] += 1


# ---------------------------------------------------------------- labeling

def _row(r) -> dict:
    return {"id": r["id"], "kind": r["kind"], "subject": r["subject"], "sender": r["sender"], "received_at": r["received_at"][:10],
            "body": r["body"][:1800], "pred": json.loads(r["pred_json"]) if r["pred_json"] else None,
            "truth": json.loads(r["truth_json"]) if r["truth_json"] else None}


def next_item(conn, after_id: int = 0, kind: str | None = None) -> dict | None:
    q = "SELECT * FROM eval_items WHERE truth_json='' AND pred_json<>'' AND id>?" + (" AND kind=?" if kind else "") + " ORDER BY id LIMIT 1"
    r = conn.execute(q, (after_id, kind) if kind else (after_id,)).fetchone()
    if r is None and after_id:
        return next_item(conn, 0, kind)             # wrap around to skipped items
    return _row(r) if r else None


def label(conn, item_id: int, truth: dict) -> None:
    status = truth.get("status") if truth.get("is_job") else None
    clean = {"is_job": bool(truth.get("is_job")), "company": (truth.get("company") or "").strip() if truth.get("is_job") else "",
             "role": (truth.get("role") or "").strip() if truth.get("is_job") else "",
             "status": evalmetrics.norm_status(status) if truth.get("is_job") else None}
    conn.execute("UPDATE eval_items SET truth_json=?, labeled_at=datetime('now') WHERE id=?", (json.dumps(clean), item_id))


def summary(conn) -> dict:
    c = lambda where: conn.execute(f"SELECT COUNT(*) FROM eval_items WHERE {where}").fetchone()[0]
    u = conn.execute("SELECT value FROM meta WHERE key='eval_universe'").fetchone()
    return {"total": c("1=1"), "predicted": c("pred_json<>''"), "labeled": c("truth_json<>''"),
            "matched_labeled": c("kind='matched' AND truth_json<>''"), "unmatched_labeled": c("kind='unmatched' AND truth_json<>''"),
            "matched_total": c("kind='matched'"), "unmatched_total": c("kind='unmatched'"),
            "universe": json.loads(u[0]) if u else None, "job": dict(state)}


# ---------------------------------------------------------------- metrics

def _labeled(conn, kind: str) -> list[dict]:
    rows = conn.execute("SELECT * FROM eval_items WHERE kind=? AND truth_json<>'' AND pred_json<>''", (kind,)).fetchall()
    return [{"id": r["id"], "subject": r["subject"], "sender": r["sender"], "body": r["body"], "truth": json.loads(r["truth_json"]),
             "pred": json.loads(r["pred_json"])} for r in rows]


def wild_stats(conn) -> dict:
    """What happened in real use: how often did you have to fix the tracker by hand?"""
    q = lambda sql: conn.execute(sql).fetchone()[0]
    email_apps = q("SELECT COUNT(*) FROM applications WHERE source='Email'")
    return {"applications_from_email": email_apps,
            "status_fixed_by_hand": q("SELECT COUNT(*) FROM applications WHERE source='Email' AND status_locked=1"),
            "in_review_queue": q("SELECT COUNT(*) FROM applications WHERE needs_review=1"),
            "emails_processed": q("SELECT COUNT(*) FROM seen_messages"),
            "emails_ignored": q("SELECT COUNT(*) FROM seen_messages WHERE outcome='ignored'")}


def report(conn) -> dict:
    matched, unmatched = _labeled(conn, "matched"), _labeled(conn, "unmatched")
    u = conn.execute("SELECT value FROM meta WHERE key='eval_universe'").fetchone()
    universe = json.loads(u[0]) if u else {"matched": len(matched), "unmatched": len(unmatched)}
    full = evalmetrics.full_report(matched, unmatched, universe["matched"], universe["unmatched"])
    # ablation: the same emails through the rules alone (no LLM), to show what the LLM adds
    extra = _blocked(conn)
    rules_items = [{"truth": i["truth"], "pred": _predict(i["subject"], i["sender"], i["body"], use_llm=False, extra_blocked=extra)} for i in matched]
    rules = {"detection": evalmetrics.detection(rules_items), "extraction": evalmetrics.extraction(rules_items)}
    return {**full, "rules_only": rules, "wild": wild_stats(conn), "universe": universe, "model": config.GROQ_MODEL}


# ---------------------------------------------------------------- markdown reports

def _pct(r: dict | None) -> str:
    if not r or r.get("value") is None:
        return "n/a"
    return f"{r['value'] * 100:.0f}% ({r['k']}/{r['n']}, 95% CI {r['ci'][0] * 100:.0f}–{r['ci'][1] * 100:.0f}%)"


def _f(x) -> str:
    return "n/a" if x is None else f"{x * 100:.0f}%"


def markdown(rep: dict) -> str:
    d, e, s, rules, w, srch = rep["detection"], rep["extraction"], rep["status"], rep["rules_only"], rep["wild"], rep["search"]
    L = ["# Evaluation", "",
         f"Measured on **{rep['n_matched_labeled']} real emails** the sync search matched and **{rep['n_unmatched_labeled']}** it did not, "
         f"hand-labeled by the mailbox owner. Model: `{rep['model']}`. Intervals are 95% Wilson confidence intervals; with samples this "
         "small, read the interval, not just the point estimate.", "",
         "## Is it an application email? (detection)", "",
         "| | Precision | Recall | F1 |", "|---|---|---|---|",
         f"| Rules + LLM | {_f(d['precision'])} | {_f(d['recall'])} | {_f(d['f1'])} |",
         f"| Rules only | {_f(rules['detection']['precision'])} | {_f(rules['detection']['recall'])} | {_f(rules['detection']['f1'])} |", "",
         "## What does it extract? (over real application emails; a missed email counts as wrong)", "",
         "| | Rules + LLM | Rules only |", "|---|---|---|",
         f"| Status | {_pct(e['status'])} | {_pct(rules['extraction']['status'])} |",
         f"| Company | {_pct(e['company'])} | {_pct(rules['extraction']['company'])} |",
         f"| Role | {_pct(e['role'])} | {_pct(rules['extraction']['role'])} |", "",
         f"Status accuracy once an email is caught: {_pct(e['status_given_caught'])}.", "",
         "## Status by class (rules + LLM)", "", "| Status | Precision | Recall | F1 | Emails |", "|---|---|---|---|---|"]
    for c, v in s["per_class"].items():
        if v["support"] or v["fp"]:
            L.append(f"| {c} | {_f(v['precision'])} | {_f(v['recall'])} | {_f(v['f1'])} | {v['support']} |")
    L += ["", "Confusion matrix (rows = truth, columns = prediction; *Missed* = not detected):", "",
          "| truth \\ pred | " + " | ".join(s["columns"]) + " |", "|---|" + "---|" * len(s["columns"])]
    for t, row in s["confusion"].items():
        L.append(f"| {t} | " + " | ".join(str(row.get(c, 0)) for c in s["columns"]) + " |")
    L += ["", "## Does the Gmail search find them? (search recall)", "",
          f"Of {srch['unmatched_universe']} emails the search did *not* match in the same period, a random sample of "
          f"{srch['unmatched_sample']['n']} contained {srch['unmatched_sample']['k']} real application emails "
          f"({_pct(srch['unmatched_sample'])}). Scaled up: about **{srch['estimated_found']} found vs {srch['estimated_missed']} missed**, "
          f"an estimated search recall of **{_f(srch['recall'])}**.", "",
          "## In real use", "",
          f"- {w['applications_from_email']} applications were created automatically from {w['emails_processed']} emails processed "
          f"({w['emails_ignored']} ignored as not application mail).",
          f"- The owner corrected the status by hand on {w['status_fixed_by_hand']} of them "
          f"({(w['status_fixed_by_hand'] / w['applications_from_email'] * 100) if w['applications_from_email'] else 0:.0f}%); "
          f"{w['in_review_queue']} are waiting in the review queue.", ""]
    return "\n".join(L)


def errors_markdown(conn) -> str:
    """Per-email failures with the text (local only, gitignored): the raw material for improving the parser."""
    L = ["# Evaluation errors (private: contains email text)", ""]
    for i in _labeled(conn, "matched"):
        t, p = i["truth"], i["pred"]
        problems = []
        if t["is_job"] != p["is_job"]:
            problems.append("MISSED a real application email" if t["is_job"] else "FALSE POSITIVE (not an application email)")
        elif t["is_job"]:
            if evalmetrics.norm_status(t["status"]) != evalmetrics.norm_status(p["status"]):
                problems.append(f"status: truth {t['status']} vs pred {p['status']}")
            if not evalmetrics.company_match(t["company"], p["company"]):
                problems.append(f"company: truth {t['company']!r} vs pred {p['company']!r}")
            if not evalmetrics.role_match(t["role"], p["role"]):
                problems.append(f"role: truth {t['role']!r} vs pred {p['role']!r}")
        if problems:
            L += [f"## {i['subject'][:100]}", f"From: {i['sender'][:80]}", *[f"- {x}" for x in problems], "", i["body"][:500].replace("\n", " "), ""]
    for i in _labeled(conn, "unmatched"):
        if i["truth"]["is_job"]:
            L += [f"## SEARCH MISS: {i['subject'][:100]}", f"From: {i['sender'][:80]}", "", i["body"][:500].replace("\n", " "), ""]
    return "\n".join(L)
