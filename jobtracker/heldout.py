"""A held-out evaluation: emails the parser was never tuned on, labeled blind, against a parser that is frozen.

  freeze()           record the cutoff date and a hash of the parser/search code; nothing the parser is judged on may change after this
  start_prospective  every email received on or after the cutoff (all matched ones, all suspects, plus a random sample of the rest)
  start_older        a random sample of matched emails from before the cutoff that were not in the tuning sample
  report()           held-out and in-sample numbers side by side

The protocol (and why it is set up this way) is in docs/EVAL_PROTOCOL.md.
"""
import hashlib
import json
import random
import subprocess
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import config, db, evalmetrics, evalset, gmail
from .parser import clean_body

ROOT = Path(__file__).resolve().parent.parent
FREEZE_FILES = ("jobtracker/parser.py", "jobtracker/gmail.py")
HELDOUT_SPLITS = ("heldout_new", "heldout_old")


# ---------------------------------------------------------------- freeze

def _hashes() -> dict[str, str]:
    return {f: hashlib.sha256((ROOT / f).read_bytes()).hexdigest() for f in FREEZE_FILES}


def _git_head() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return ""


def _meta(conn, key: str):
    r = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(r[0]) if r else None


def _set_meta(conn, key: str, value: dict) -> None:
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, json.dumps(value)))


def freeze(conn, cutoff: date) -> dict:
    """Record the cutoff and the code hashes. Refused once held-out emails have been labeled: the freeze can't move after that."""
    if conn.execute("SELECT 1 FROM eval_items WHERE split IN ('heldout_new','heldout_old') AND truth_json<>'' LIMIT 1").fetchone():
        raise ValueError("Held-out emails are already labeled, so the freeze can no longer be changed.")
    rec = {"cutoff": cutoff.isoformat(), "files": _hashes(), "commit": _git_head(),
           "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    _set_meta(conn, "eval_freeze", rec)
    return rec


def freeze_status(conn) -> dict:
    rec = _meta(conn, "eval_freeze")
    if not rec:
        return {"frozen": False}
    now = _hashes()
    changed = [f for f in FREEZE_FILES if now[f] != rec["files"].get(f)]
    return {"frozen": True, "cutoff": rec["cutoff"], "commit": rec["commit"], "at": rec["at"],
            "unchanged": not changed, "changed_files": changed, "files": rec["files"]}


def _require_freeze(conn) -> date:
    st = freeze_status(conn)
    if not st["frozen"]:
        raise ValueError("Freeze the parser first.")
    if not st["unchanged"]:
        raise ValueError(f"The frozen code has changed ({', '.join(st['changed_files'])}); held-out results would not describe a fixed parser.")
    return date.fromisoformat(st["cutoff"])


# ---------------------------------------------------------------- sampling (background)

def _start(phase: str, fn, *args) -> bool:
    if not evalset._lock.acquire(blocking=False):
        return False
    evalset.state.update(running=True, phase=phase, done=0, total=0, error="")

    def run():
        try:
            fn(*args)
        except Exception as e:
            evalset.state["error"] = f"{type(e).__name__}: {e}"
        finally:
            evalset.state.update(running=False, phase="Done" if not evalset.state["error"] else evalset.state["phase"])
            evalset._lock.release()
    threading.Thread(target=run, daemon=True).start()
    return True


def _store(svc, ids_kinds: list[tuple[str, str]], split: str, keep) -> int:
    """Download each email and store it for labeling; `keep(received_at)` filters by date (the search window is padded). Returns how many were kept."""
    evalset.state.update(phase="Downloading emails…", total=len(ids_kinds), done=0)
    kept = 0
    for gid, kind in ids_kinds:
        msg = gmail.fetch_message(svc, gid)
        if keep(msg["received_at"][:10]):
            with db.session() as conn:
                conn.execute("INSERT OR IGNORE INTO eval_items (gmail_id, kind, subject, sender, received_at, body, split) VALUES (?,?,?,?,?,?,?)",
                             (gid, kind, msg["subject"][:300], msg["sender"][:200], msg["received_at"],
                              clean_body(msg["body"], evalset.MAX_BODY), split))
            kept += 1
        evalset.state["done"] += 1
    return kept


def start_prospective(n_cold: int = 200, seed: int = 11) -> bool:
    with db.session() as conn:
        cutoff = _require_freeze(conn)
    return _start("Searching Gmail…", _run_prospective, cutoff, n_cold, seed)


def _run_prospective(cutoff: date, n_cold: int, seed: int):
    svc = gmail.service()
    today = date.today()
    qm, qu = evalset.queries(cutoff, today)
    in_window = lambda d: cutoff.isoformat() <= d
    m_ids, u_ids = evalset._ids(svc, qm), evalset._ids(svc, qu)
    evalset.state.update(phase="Reading subjects and senders…", total=len(u_ids), done=0)
    heads = gmail.fetch_headers(svc, u_ids, on_progress=lambda n: evalset.state.update(done=n))
    hot = [i for i in u_ids if i in heads and evalset.is_hot(heads[i]["subject"], heads[i]["sender"])]
    hot_set = set(hot)
    cold = [i for i in u_ids if i in heads and i not in hot_set]
    with db.session() as conn:
        have = {r[0] for r in conn.execute("SELECT gmail_id FROM eval_items")}
        _set_meta(conn, "eval_universe_prospective", {
            "matched": len(m_ids), "unmatched": len(u_ids), "n_hot": len(hot), "n_cold": len(cold), "since": cutoff.isoformat(),
            "until": today.isoformat(), "query_version": gmail.QUERY_VERSION, "headers_read": len(heads),
            "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
    rng = random.Random(seed)
    fresh_cold = [i for i in cold if i not in have]
    picks = [(i, "matched") for i in m_ids if i not in have] + [(i, "unmatched_hot") for i in hot if i not in have]
    picks += [(i, "unmatched") for i in rng.sample(fresh_cold, min(n_cold, len(fresh_cold)))]
    _store(svc, picks, "heldout_new", in_window)
    evalset._predict_pending()


def start_older(n: int = 200, seed: int = 13) -> bool:
    with db.session() as conn:
        cutoff = _require_freeze(conn)
    return _start("Searching Gmail…", _run_older, cutoff, n, seed)


def _run_older(cutoff: date, n: int, seed: int):
    svc = gmail.service()
    with db.session() as conn:
        u = _meta(conn, "eval_universe")
        have = {r[0] for r in conn.execute("SELECT gmail_id FROM eval_items")}
    since = date.fromisoformat(u["since"]) if u and u.get("since") else date.today() - timedelta(days=120)
    before_cutoff = lambda d: d < cutoff.isoformat()
    qm, _ = evalset.queries(since, cutoff - timedelta(days=1))
    m_ids = evalset._ids(svc, qm)
    pool = [i for i in m_ids if i not in have]
    picks = [(i, "matched") for i in random.Random(seed).sample(pool, min(n, len(pool)))]
    with db.session() as conn:
        _set_meta(conn, "eval_universe_older", {"matched": len(m_ids), "already_in_tuning_sample": len(m_ids) - len(pool),
                                               "since": since.isoformat(), "until": (cutoff - timedelta(days=1)).isoformat(),
                                               "query_version": gmail.QUERY_VERSION})
    _store(svc, picks, "heldout_old", before_cutoff)
    evalset._predict_pending()


# ---------------------------------------------------------------- metrics

def _split_report(conn, splits: tuple) -> dict | None:
    matched = evalset._labeled(conn, "matched", splits)
    if not matched:
        return None
    extra = evalset._blocked(conn)
    rules_items = [{"truth": i["truth"], "pred": evalset._predict(i["subject"], i["sender"], i["body"], use_llm=False, extra_blocked=extra)} for i in matched]
    return {"n": len(matched), "detection": evalmetrics.detection(matched), "extraction": evalmetrics.extraction(matched),
            "status": evalmetrics.status_breakdown(matched),
            "rules_only": {"detection": evalmetrics.detection(rules_items), "extraction": evalmetrics.extraction(rules_items)}}


def progress(conn) -> dict:
    c = lambda split, where="1=1": conn.execute(f"SELECT COUNT(*) FROM eval_items WHERE split=? AND {where}", (split,)).fetchone()[0]
    return {s: {"total": c(s), "labeled": c(s, "truth_json<>''"), "blind": c(s, "truth_json<>'' AND blind=1")} for s in HELDOUT_SPLITS}


def since_freeze(conn, cutoff: str) -> dict:
    """End to end, without any extra labeling: applications whose first email came after the freeze, and how many needed a manual status fix."""
    q = lambda sql: conn.execute(sql, (cutoff,)).fetchone()[0]
    return {"applications": q("SELECT COUNT(*) FROM applications WHERE source='Email' AND applied_at>=?"),
            "status_fixed_by_hand": q("SELECT COUNT(*) FROM applications WHERE source='Email' AND applied_at>=? AND status_locked=1")}


def report(conn) -> dict:
    st = freeze_status(conn)
    prog = progress(conn)
    out = {"freeze": st, "progress": prog, "heldout": _split_report(conn, HELDOUT_SPLITS), "prospective": _split_report(conn, ("heldout_new",)),
           "older": _split_report(conn, ("heldout_old",)), "search": None, "since_freeze": since_freeze(conn, st["cutoff"]) if st["frozen"] else None}
    uni = _meta(conn, "eval_universe_prospective")
    if uni:
        s = ("heldout_new",)
        matched = evalset._labeled(conn, "matched", s)
        hot_pool = evalset._labeled(conn, "unmatched_hot", s) + evalset._labeled(conn, "unmatched", s)
        hot = [i for i in hot_pool if evalset.is_hot(i["subject"], i["sender"])]
        cold = [i for i in evalset._labeled(conn, "unmatched", s) if not evalset.is_hot(i["subject"], i["sender"])]
        if matched and cold:
            out["search"] = evalmetrics.search_recall_stratified(matched, cold, hot, uni["matched"], uni["n_hot"], uni["n_cold"])
            out["search_universe"] = uni
    return out


def markdown(rep: dict, tuning_md: str = "") -> str:
    fz, prog, h = rep["freeze"], rep["progress"], rep["heldout"]
    n_lab = sum(p["labeled"] for p in prog.values())
    n_blind = sum(p["blind"] for p in prog.values())
    L = ["# Evaluation", "",
         "> **Author's results, on the author's mailbox.** Part 1 is a *held-out* test: emails the parser was never tuned on, scored against a "
         "parser that was frozen before they were labeled. Part 2 is the earlier in-sample result the parser was tuned on, kept for comparison only. "
         "Protocol: `docs/EVAL_PROTOCOL.md`.", ""]
    if fz.get("frozen"):
        L += [f"Frozen at commit `{fz['commit']}` on {fz['at'][:10]}; held-out window starts {fz['cutoff']}. "
              f"The frozen code {'still matches' if fz['unchanged'] else '**no longer matches**'} the recorded hashes. "
              f"{n_blind} of {n_lab} held-out labels were made blind (the parser's guess was hidden).", ""]
    L += ["## Part 1: held-out results", ""]
    if not h:
        L += ["No held-out emails are labeled yet.", ""]
    else:
        L += [f"**{h['n']} labeled emails**: {prog['heldout_new']['labeled']} received after the freeze, {prog['heldout_old']['labeled']} older ones "
              "that were not in the tuning sample. Intervals are 95% Wilson intervals.", ""]
        L += evalset.metric_tables(h, "###")
        for name, key in (("Received after the freeze", "prospective"), ("Older emails", "older")):
            r = rep.get(key)
            if r:
                e = r["extraction"]
                L += ["", f"{name}: {r['n']} emails, status {evalset._pct(e['status'])}, company {evalset._pct(e['company'])}, role {evalset._pct(e['role'])}."]
    if rep.get("search"):
        L += ["", "### Does the Gmail search find them? (search recall, emails after the freeze)", ""]
        L += evalset.search_lines(rep["search"], " received after the freeze")
    sf = rep.get("since_freeze")
    if sf:
        pct = sf["status_fixed_by_hand"] / sf["applications"] * 100 if sf["applications"] else 0
        L += ["", "### In real use since the freeze", "",
              f"- {sf['applications']} applications were created from email; the owner corrected the status by hand on {sf['status_fixed_by_hand']} ({pct:.0f}%).", ""]
    if tuning_md:
        L += ["", "---", "", tuning_md.replace("# Evaluation", "# Part 2: in-sample results (the parser was tuned on these)", 1)]
    return "\n".join(L)
