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


def queries(since: date, until: date | None = None) -> tuple[str, str]:
    matched = gmail.build_query(since, until)
    base = f"after:{(since - timedelta(days=1)):%Y/%m/%d} -from:me -in:spam -in:trash"
    if until:
        base += f" before:{(until + timedelta(days=2)):%Y/%m/%d}"
    unmatched = f"{base} -{gmail.matched_core()}"
    return matched, unmatched


def _predict(subject: str, sender: str, body: str, use_llm: bool, extra_blocked: tuple = ()) -> dict:
    p = parse_email(subject, sender, body, use_llm=use_llm, extra_blocked=extra_blocked)
    is_job = bool(p.is_job and p.company)            # exactly the sync's rule for "this becomes an application"
    return {"is_job": is_job, "company": p.company if is_job else "", "role": p.role if is_job else "",
            "status": p.status if is_job else None, "via": p.via}


def _blocked(conn) -> tuple:
    return tuple(r[0] for r in conn.execute("SELECT pattern FROM blocked_senders"))


def is_hot(subject: str, sender: str) -> bool:
    """Could this unmatched email plausibly be application mail? Judged from sender and subject alone (cheap, recall-oriented):
    a recruiting-system sender or job words in the subject. Job boards and other known noise don't count."""
    from .parser import _JOB_SIGNAL, is_ats, is_blocked
    if is_blocked(sender, subject):
        return False
    return is_ats(sender) or bool(_JOB_SIGNAL.search(subject))


def start_scan(since: date) -> bool:
    """Read the subject + sender of EVERY email the search did not match, and queue the plausible ones for labeling."""
    if not _lock.acquire(blocking=False):
        return False
    state.update(running=True, phase="Listing unmatched mail…", done=0, total=0, error="")
    threading.Thread(target=_run_scan, args=(since,), daemon=True).start()
    return True


def _run_scan(since):
    try:
        svc = gmail.service()
        _, qu = queries(since)
        u_ids = _ids(svc, qu)
        state.update(phase="Reading subjects and senders…", total=len(u_ids), done=0)
        heads = gmail.fetch_headers(svc, u_ids, on_progress=lambda n: state.update(done=n))
        hot = [i for i in u_ids if i in heads and is_hot(heads[i]["subject"], heads[i]["sender"])]
        with db.session() as conn:
            have = {r[0] for r in conn.execute("SELECT gmail_id FROM eval_items")}
            u = conn.execute("SELECT value FROM meta WHERE key='eval_universe'").fetchone()
            uni = json.loads(u[0]) if u else {}
            uni.setdefault("query_version", gmail.QUERY_VERSION)
            uni.update(unmatched=len(u_ids), n_hot=len(hot), n_cold=len(u_ids) - len(hot), since=uni.get("since") or since.isoformat(),
                       scan_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), headers_read=len(heads))
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('eval_universe', ?)", (json.dumps(uni),))
        todo = [i for i in hot if i not in have]
        state.update(phase="Downloading suspects…", total=len(todo), done=0)
        for gid in todo:
            msg = gmail.fetch_message(svc, gid)
            with db.session() as conn:
                conn.execute("INSERT OR IGNORE INTO eval_items (gmail_id, kind, subject, sender, received_at, body) VALUES (?,?,?,?,?,?)",
                             (gid, "unmatched_hot", msg["subject"][:300], msg["sender"][:200], msg["received_at"], clean_body(msg["body"], MAX_BODY)))
            state["done"] += 1
        _predict_pending()
    except Exception as e:
        state["error"] = f"{type(e).__name__}: {e}"
    finally:
        state.update(running=False, phase="Done" if not state["error"] else state["phase"])
        _lock.release()


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
                 "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "query_version": gmail.QUERY_VERSION}),))
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


def start_predict(redo: bool = False) -> bool:
    if not _lock.acquire(blocking=False):
        return False
    state.update(running=True, phase="Running the pipeline…", done=0, total=0, error="")

    def run():
        try:
            _predict_pending(redo=redo)
        except Exception as e:
            state["error"] = f"{type(e).__name__}: {e}"
        finally:
            state.update(running=False, phase="Done" if not state["error"] else state["phase"])
            _lock.release()
    threading.Thread(target=run, daemon=True).start()
    return True


def _predict_pending(redo: bool = False):
    """Fill in missing predictions; with redo, recompute all of them in place (old ones stay until replaced)."""
    with db.session() as conn:
        rows = conn.execute(f"SELECT id, kind, subject, sender, body FROM eval_items {'' if redo else 'WHERE pred_json=' + chr(39) * 2} ORDER BY id").fetchall()
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

def _row(r, blind: bool = False) -> dict:
    pred = json.loads(r["pred_json"]) if r["pred_json"] else None
    if blind and pred:                    # blind labeling: keep the company and role guess to correct, hide whether-it-is-an-application and status
        pred = {**pred, "is_job": None, "status": None, "via": ""}
    return {"id": r["id"], "kind": r["kind"], "split": r["split"], "subject": r["subject"], "sender": r["sender"], "received_at": r["received_at"][:10],
            "body": r["body"][:1800], "pred": pred, "truth": json.loads(r["truth_json"]) if r["truth_json"] else None}


def next_item(conn, after_id: int = 0, kind: str | None = None, split: str | None = None, blind: bool = False) -> dict | None:
    q = ("SELECT * FROM eval_items WHERE truth_json='' AND pred_json<>'' AND id>?" + (" AND kind=?" if kind else "")
         + (" AND split IN ('heldout_new','heldout_old')" if split == "heldout" else " AND split=?" if split else "") + " ORDER BY id LIMIT 1")
    r = conn.execute(q, (after_id, *([kind] if kind else []), *([split] if split and split != "heldout" else []))).fetchone()
    if r is None and after_id:
        return next_item(conn, 0, kind, split, blind)    # wrap around to skipped items
    return _row(r, blind) if r else None


def get_item(conn, item_id: int, blind: bool = False) -> dict | None:
    r = conn.execute("SELECT * FROM eval_items WHERE id=?", (item_id,)).fetchone()
    return _row(r, blind) if r else None


def list_labeled(conn, limit: int = 400) -> list[dict]:
    """Labeled emails, newest label first, with whether you corrected the pipeline's guess."""
    out = []
    for r in conn.execute("SELECT * FROM eval_items WHERE truth_json<>'' ORDER BY labeled_at DESC, id DESC LIMIT ?", (limit,)):
        t, p = json.loads(r["truth_json"]), (json.loads(r["pred_json"]) if r["pred_json"] else None)
        same = bool(p) and t["is_job"] == p["is_job"] and (not t["is_job"] or (
            evalmetrics.norm_status(t["status"]) == evalmetrics.norm_status(p["status"])
            and evalmetrics.company_match(t["company"], p["company"]) and evalmetrics.role_match(t["role"], p["role"])))
        out.append({"id": r["id"], "kind": r["kind"], "subject": r["subject"], "sender": r["sender"], "received_at": r["received_at"][:10],
                    "truth": t, "corrected": not same, "labeled_at": r["labeled_at"]})
    return out


def unlabel(conn, item_id: int) -> None:
    conn.execute("UPDATE eval_items SET truth_json='', labeled_at=NULL, blind=0 WHERE id=?", (item_id,))


def label(conn, item_id: int, truth: dict, blind: bool = False) -> None:
    status = truth.get("status") if truth.get("is_job") else None
    clean = {"is_job": bool(truth.get("is_job")), "company": (truth.get("company") or "").strip() if truth.get("is_job") else "",
             "role": (truth.get("role") or "").strip() if truth.get("is_job") else "",
             "status": evalmetrics.norm_status(status) if truth.get("is_job") else None}
    conn.execute("UPDATE eval_items SET truth_json=?, labeled_at=datetime('now'), blind=? WHERE id=?", (json.dumps(clean), int(blind), item_id))


def summary(conn) -> dict:
    c = lambda where: conn.execute(f"SELECT COUNT(*) FROM eval_items WHERE {where}").fetchone()[0]
    u = conn.execute("SELECT value FROM meta WHERE key='eval_universe'").fetchone()
    return {"total": c("1=1"), "predicted": c("pred_json<>''"), "labeled": c("truth_json<>''"),
            "matched_labeled": c("kind='matched' AND truth_json<>''"), "unmatched_labeled": c("kind='unmatched' AND truth_json<>''"),
            "matched_total": c("kind='matched'"), "unmatched_total": c("kind='unmatched'"),
            "hot_total": c("kind='unmatched_hot'"), "hot_labeled": c("kind='unmatched_hot' AND truth_json<>''"),
            "universe": json.loads(u[0]) if u else None, "job": dict(state)}


# ---------------------------------------------------------------- metrics

def _labeled(conn, kind: str, splits: tuple = ("tuning",)) -> list[dict]:
    """Labeled + predicted items of one kind, limited to the given splits (default: only the tuning sample, as before)."""
    marks = ",".join("?" * len(splits))
    rows = conn.execute(f"SELECT * FROM eval_items WHERE kind=? AND split IN ({marks}) AND truth_json<>'' AND pred_json<>''", (kind, *splits)).fetchall()
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
    if universe.get("n_hot") is not None:         # a full scan of the unmatched mail was done: use the two-strata estimate
        pool = unmatched + _labeled(conn, "unmatched_hot")
        hot = [i for i in pool if is_hot(i["subject"], i["sender"])]
        cold = [i for i in unmatched if not is_hot(i["subject"], i["sender"])]
        full["search"] = evalmetrics.search_recall_stratified(matched, cold, hot, universe["matched"], universe["n_hot"], universe["n_cold"])
        full["n_unmatched_labeled"] = len(unmatched) + len(_labeled(conn, "unmatched_hot"))
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


def metric_tables(rep: dict, level: str = "##") -> list[str]:
    """Detection, extraction, per-class and confusion tables for one split's report (used by the held-out report)."""
    d, e, s, rules = rep["detection"], rep["extraction"], rep["status"], rep["rules_only"]
    L = [f"{level} Is it an application email? (detection)", "",
         "| | Precision | Recall | F1 |", "|---|---|---|---|",
         f"| Rules + LLM | {_f(d['precision'])} | {_f(d['recall'])} | {_f(d['f1'])} |",
         f"| Rules only | {_f(rules['detection']['precision'])} | {_f(rules['detection']['recall'])} | {_f(rules['detection']['f1'])} |", "",
         f"{level} What does it extract? (over real application emails; a missed email counts as wrong)", "",
         "| | Rules + LLM | Rules only |", "|---|---|---|",
         f"| Status | {_pct(e['status'])} | {_pct(rules['extraction']['status'])} |",
         f"| Company | {_pct(e['company'])} | {_pct(rules['extraction']['company'])} |",
         f"| Role | {_pct(e['role'])} | {_pct(rules['extraction']['role'])} |", "",
         f"Status accuracy once an email is caught: {_pct(e['status_given_caught'])}.", "",
         f"{level} Status by class (rules + LLM)", "",
         "| Status | Precision | Recall (95% CI) | F1 | Emails |", "|---|---|---|---|---|"]
    for c, v in s["per_class"].items():
        if v["support"] or v["fp"]:
            ci = v.get("recall_ci")
            rec = _f(v["recall"]) + (f" ({ci[0] * 100:.0f}–{ci[1] * 100:.0f}%)" if ci else "")
            L.append(f"| {c} | {_f(v['precision'])} | {rec} | {_f(v['f1'])} | {v['support']} |")
    if s.get("macro_f1") is not None:
        L += ["", f"Macro-averaged F1 over the {s['classes_with_examples']} classes that have examples: **{_f(s['macro_f1'])}**. "
              "Classes with only a few examples have very wide intervals; read them as anecdotes, not measurements."]
    L += ["", "Confusion matrix (rows = truth, columns = prediction; *Missed* = not detected):", "",
          "| truth \\ pred | " + " | ".join(s["columns"]) + " |", "|---|" + "---|" * len(s["columns"])]
    for t, row in s["confusion"].items():
        L.append(f"| {t} | " + " | ".join(str(row.get(c, 0)) for c in s["columns"]) + " |")
    return L


def search_lines(srch: dict, window: str = "") -> list[str]:
    """The search-recall paragraph for a stratified estimate (used by the held-out report)."""
    ci = srch.get("recall_ci")
    rng = f"{_f(ci[0]) if ci else 'n/a'} to {_f(ci[1]) if ci else 'n/a'}"
    h, c = srch["hot"], srch["cold"]
    return [f"I read the subject and sender of **all {srch['unmatched_universe']}** emails the search did not match{window} and flagged **{h['universe']}** "
            f"as plausible application mail. Of those, {h['labeled']} were labeled by hand and **{h['job']} were real application emails**"
            + (" (every suspect was checked, so this count is exact)." if h["exact"] else " (the rest are estimated)."), "",
            f"The other {c['universe']} look unrelated; a random sample of {c['labeled']} of them contained {c['job']} real application emails ({_pct(c['rate'])}).", "",
            f"Estimate: about **{srch['estimated_found']} found vs {srch['estimated_missed']} missed**, search recall **{_f(srch['recall'])}** "
            f"(95% range **{rng}**; at most about {srch['missed_upper_bound']} missed).", ""]


def markdown(rep: dict) -> str:
    d, e, s, rules, w, srch = rep["detection"], rep["extraction"], rep["status"], rep["rules_only"], rep["wild"], rep["search"]
    L = ["# Evaluation", "",
         "> **These are the author's results, on the author's mailbox.** Accuracy depends on your own mail (employers, recruiting systems, "
         "language), so don't treat these figures as a guarantee. To measure yours, set `ENABLE_EVAL=1` in `.env`, open the **Evaluate** tab, "
         "sample, label and compute metrics. The parser was tuned on part of this sample, so the figures are optimistic until re-measured on a "
         "fresh held-out sample.", "",
         f"Measured on **{rep['n_matched_labeled']} real emails** the sync search matched and **{rep['n_unmatched_labeled']}** it did not, "
         f"hand-labeled by the mailbox owner. Model: `{rep['model']}`. Intervals are 95% Wilson confidence intervals; with samples this "
         "small, read the interval, not just the point estimate.", ""]
    L += metric_tables(rep)
    ci = srch.get("recall_ci")
    rng = f"{_f(ci[0]) if ci else 'n/a'} to {_f(ci[1]) if ci else 'n/a'}"
    if srch.get("stratified"):
        search_text = [para for para in search_lines(srch) if para]
    else:
        search_text = [
            f"Of {srch['unmatched_universe']} emails the search did *not* match in the same period, a random sample of "
            f"{srch['unmatched_sample']['n']} contained {srch['unmatched_sample']['k']} real application emails ({_pct(srch['unmatched_sample'])}). "
            f"Point estimate: about **{srch['estimated_found']} found vs {srch['estimated_missed']} missed** (search recall **{_f(srch['recall'])}**). "
            f"Because the sample is small, the honest range is **{rng}**, i.e. up to about {srch['missed_upper_bound']} application emails could be "
            "hiding in the unmatched mail; a larger or targeted sample is needed to narrow this."]
    L += ["", "## Does the Gmail search find them? (search recall)", ""]
    qv = (rep.get("universe") or {}).get("query_version", 1)
    if qv < gmail.QUERY_VERSION:
        L += [f"*These figures describe search version {qv}. The search has since been widened (version {gmail.QUERY_VERSION}: recruiting-system "
              "senders in every Gmail tab and extra wording) and now matches the real emails listed as missed below. A fresh sample is needed to "
              "measure the current search.*", ""]
    for para in search_text:
        L += [para, ""]
    L += [
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
    for i in _labeled(conn, "unmatched") + _labeled(conn, "unmatched_hot"):
        if i["truth"]["is_job"]:
            L += [f"## SEARCH MISS: {i['subject'][:100]}", f"From: {i['sender'][:80]}", "", i["body"][:500].replace("\n", " "), ""]
    return "\n".join(L)
