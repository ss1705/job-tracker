"""Pull emails for a date range, parse them, and fold them into applications/events."""
import json
import threading
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from datetime import date

from . import config, db, gmail, milestones, outreach
from .parser import LLMExhausted, Parsed, action_summary, is_receipt_subject, parse_email

# lifecycle ranking used to decide the "current" status of an application
_ORDER = {"Saved": 0, "Applied": 1, "OA": 2, "Interview": 3, "Offer": 4}


def derive_status(events: list[tuple[str, str]]) -> str:
    """events: [(received_at, type)]. Offer is final. Otherwise the latest meaningful event wins, except:
    a stray late 'Applied' receipt never downgrades progress, and once rejected, only a later
    Interview/Offer reopens the application (late OA/reminder emails don't)."""
    meaningful = sorted((e for e in events if e[1] in config.STATUSES), key=lambda e: e[0])
    if not meaningful:
        return "Applied"
    if any(t == "Offer" for _, t in meaningful):
        return "Offer"
    rejected_at = max((ts for ts, t in meaningful if t == "Rejected"), default=None)
    if rejected_at:
        reopened = [t for ts, t in meaningful if ts > rejected_at and t == "Interview"]
        if not reopened:
            return "Rejected"
    non_applied = [t for _, t in meaningful if t != "Applied"]
    return non_applied[-1] if non_applied else "Applied"


def local_date(utc_iso: str) -> str:
    """Emails are stored in UTC; the 'applied on' date should be the day it was in the user's timezone."""
    dt = datetime.fromisoformat(utc_iso).replace(tzinfo=timezone.utc)
    dt = dt.astimezone(ZoneInfo(config.TIMEZONE)) if config.TIMEZONE else dt.astimezone()
    return dt.strftime("%Y-%m-%d")


def recompute(conn, app_id: int):
    app = conn.execute("SELECT status_locked FROM applications WHERE id=?", (app_id,)).fetchone()
    rows = conn.execute("SELECT received_at, type FROM events WHERE application_id=?", (app_id,)).fetchall()
    times = [r["received_at"] for r in rows]
    applied = [r["received_at"] for r in rows if r["type"] == "Applied"]
    fields = {"last_event_at": max(times) if times else None}
    if applied:
        fields["applied_at"] = local_date(min(applied))
    if not app["status_locked"]:
        fields["status"] = derive_status([(r["received_at"], r["type"]) for r in rows])
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE applications SET {sets} WHERE id=?", (*fields.values(), app_id))


def apply_parsed(conn, msg: dict, p: Parsed, out: dict | None = None) -> str:
    """Insert the event (creating/matching an application). Returns the outcome label; `out["app_id"]` receives the application id."""
    is_applied = p.status == "Applied"
    app, confident = db.find_application(conn, p.company, p.role, p.req_id, is_applied)
    review = int(not confident or not p.status)

    # A role-less receipt on a different day than an earlier receipt at the same company may be a *different*
    # job (generic ATS confirmations don't name the role). Don't silently merge: make a new row, flag it.
    force_new = False
    if app is not None and is_applied and not p.role and is_receipt_subject(msg["subject"]):
        mine = local_date(msg["received_at"])
        earlier = conn.execute(
            "SELECT received_at, subject FROM events WHERE application_id=? AND type='Applied'", (app["id"],)).fetchall()
        if any(is_receipt_subject(e["subject"]) and local_date(e["received_at"]) != mine for e in earlier):
            force_new, app, review = True, None, 1

    if app is None:
        cur = conn.execute(
            """INSERT INTO applications (company, company_key, role, req_id, job_type, source, status, needs_review)
               VALUES (?,?,?,?,?,?,?,?)""",
            (p.company, db.company_key(p.company), p.role, p.req_id, p.job_type, "Email",
             p.status if p.status in config.STATUSES else "Applied", int(not p.status or force_new)),
        )
        app_id = cur.lastrowid
    else:
        app_id = app["id"]
        # enrich blanks, never overwrite what the user or an earlier email set
        conn.execute(
            """UPDATE applications SET
                 role = CASE WHEN role = '' THEN ? ELSE role END,
                 req_id = CASE WHEN req_id = '' THEN ? ELSE req_id END,
                 job_type = CASE WHEN job_type = '' THEN ? ELSE job_type END,
                 needs_review = needs_review OR ?
               WHERE id=?""",
            (p.role, p.req_id, p.job_type, review, app_id),
        )

    conn.execute(
        "INSERT INTO events (application_id, gmail_id, type, received_at, subject, sender, snippet) VALUES (?,?,?,?,?,?,?)",
        (app_id, msg["id"], p.status or "Update", msg["received_at"], msg["subject"][:300], msg["sender"][:200],
         " ".join(msg["body"].split())[:400]),
    )
    recompute(conn, app_id)
    day = local_date(msg["received_at"])
    if p.status == "Action":          # they need something from you: flag it until you mark it done or a later email shows progress
        conn.execute("UPDATE applications SET action_note=?, action_at=?, action_done=0 WHERE id=?",
                     (action_summary(msg["subject"], msg["body"]), day, app_id))
    elif p.status in ("OA", "Interview", "Offer", "Rejected"):
        conn.execute("UPDATE applications SET action_done=1 WHERE id=? AND action_at IS NOT NULL AND action_at <= ?", (app_id, day))
    elif p.status == "Applied":
        conn.execute("UPDATE applications SET action_done=1 WHERE id=? AND action_at IS NOT NULL AND action_at < ?", (app_id, day))
    if out is not None:
        out["app_id"] = app_id
    return "needs_review" if review else "linked"


def split_events(conn, app_id: int, event_ids: list[int], role: str = "") -> int:
    """Move the given emails out of an application into a new one (same company). Returns the new id."""
    app = conn.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
    mine = {r["id"] for r in conn.execute("SELECT id FROM events WHERE application_id=?", (app_id,))}
    ids = [i for i in event_ids if i in mine]
    if not app or not ids:
        raise ValueError("no matching emails to move")
    if len(ids) >= len(mine):
        raise ValueError("leave at least one email on the original application")
    cur = conn.execute(
        """INSERT INTO applications (company, company_key, role, job_type, location, source, link, status)
           VALUES (?,?,?,?,?,?,?, 'Applied')""",
        (app["company"], app["company_key"], role, app["job_type"], app["location"], app["source"], ""))
    new_id = cur.lastrowid
    conn.execute(f"UPDATE events SET application_id=? WHERE id IN ({','.join('?' * len(ids))})", (new_id, *ids))
    recompute(conn, new_id)
    recompute(conn, app_id)
    return new_id


def capture_milestones(app_id: int, msg: dict) -> int:
    """Deadlines / interview times from an OA or interview email. Optional extra: a failure here never stops a sync."""
    try:
        items = milestones.extract(msg["subject"], msg["body"], msg["received_at"])
        if not items:
            return 0
        with db.session() as conn:
            return milestones.store(conn, app_id, items, msg["received_at"])
    except Exception as e:
        print(f"[milestones] skipped: {type(e).__name__}: {e}")
        return 0


# ---------------------------------------------------------------- orchestration

def _fetch_with_retries(svc, mid, waiting, tries: int = 3):
    for attempt in range(tries):
        try:
            return gmail.fetch_message(svc, mid, on_wait=waiting)
        except (TimeoutError, OSError, ConnectionError) as e:   # network blips (httplib2 raises these)
            print(f"[sync] fetch {mid} failed ({type(e).__name__}: {e}), attempt {attempt + 1}/{tries}")
            time.sleep(2 * (attempt + 1))
    return None


class SyncState:
    def __init__(self):
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        self.running = False
        self.total = self.done = 0
        self.added = self.updated = self.ignored = self.review = self.failed = self.linkedin = 0
        self.error = ""
        self.message = ""


state = SyncState()


def run_sync(since: date, until: date | None = None, reprocess_linkedin: bool = False):
    if not state.lock.acquire(blocking=False):
        return
    try:
        state.reset()
        state.running = True
        state.message = "Searching Gmail…"
        svc = gmail.service()
        ids = gmail.list_message_ids(svc, since, until)
        ids += [i for i in gmail.list_linkedin_ids(svc, since, until) if i not in set(ids)]
        with db.session() as conn:
            seen = {r[0] for r in conn.execute("SELECT gmail_id FROM seen_messages")}
            extra_blocked = tuple(r[0] for r in conn.execute("SELECT pattern FROM blocked_senders"))
        li_ids = set(gmail.list_linkedin_ids(svc, since, until)) if reprocess_linkedin else set()
        todo = [i for i in ids if i not in seen or i in li_ids]
        state.total = len(todo)
        state.message = f"Processing {len(todo)} new emails (of {len(ids)} matches)…"

        # Gmail lists newest-first; reverse so applications are created before their later updates.
        # Each message is fetched, parsed and saved on its own, so a failure or quota stall
        # never loses progress: re-running the sync resumes where it stopped.
        def waiting(sec):
            state.message = f"Gmail rate limit hit, waiting {int(sec)}s…"

        for mid in reversed(todo):
            msg = _fetch_with_retries(svc, mid, waiting)
            if msg is None:          # transient failure: leave it unseen so the next sync retries it
                state.failed += 1
                state.done += 1
                continue
            state.message = f"Processing {len(todo)} new emails (of {len(ids)} matches)…"
            resp = outreach.parse_linkedin_email(msg)
            if resp:       # someone you reached out to on LinkedIn accepted / replied
                with db.session() as conn:
                    _, how = outreach.record_response(
                        conn, name=resp["full_name"] or resp["name"], slug=resp["slug"], when=local_date(msg["received_at"]),
                        kind=resp["kind"], create_missing=resp["kind"] == "Accepted invite", link=resp["link"].split("?")[0],
                        title=resp["title"], company=resp["company"])
                    conn.execute("INSERT OR REPLACE INTO seen_messages (gmail_id, outcome) VALUES (?,?)",
                                 (msg["id"], "linkedin" if how != "ignored" else "ignored"))
                state.linkedin += how in ("created", "updated")
                state.ignored += how == "ignored"
                state.done += 1
                continue
            try:
                p = parse_email(msg["subject"], msg["sender"], msg["body"], extra_blocked=extra_blocked)
            except LLMExhausted:
                raise
            except Exception as e:     # a parser bug or odd email must not kill the whole run
                print(f"[sync] parse failed for {mid}: {type(e).__name__}: {e}")
                state.failed += 1
                state.done += 1
                continue
            landed = {}
            with db.session() as conn:
                if not p.is_job or not p.company:
                    outcome = "ignored"
                    state.ignored += 1
                else:
                    before = conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0]
                    outcome = apply_parsed(conn, msg, p, out=landed)
                    after = conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0]
                    state.added += after - before
                    state.updated += int(after == before)
                    state.review += int(outcome == "needs_review")
                conn.execute("INSERT OR REPLACE INTO seen_messages (gmail_id, outcome) VALUES (?,?)", (msg["id"], outcome))
            if landed.get("app_id") and p.status in ("OA", "Interview", "Action"):
                capture_milestones(landed["app_id"], msg)       # after the write is committed: the LLM call can be slow
            state.done += 1
        state.message = "Done"
    except gmail.AuthRequired as e:
        state.error = str(e)
    except LLMExhausted as e:
        state.error = f"{e} ({state.done} of {state.total} emails processed this run.)"
    except Exception as e:
        state.error = f"{type(e).__name__}: {e}"
    finally:
        state.running = False
        try:
            with db.session() as conn:
                conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('last_sync', ?)", (json.dumps({
                    "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "since": since.isoformat(),
                    "added": state.added, "updated": state.updated, "ignored": state.ignored, "failed": state.failed,
                    "needs_review": state.review, "linkedin": state.linkedin, "processed": state.done, "total": state.total, "error": state.error,
                }),))
        except Exception as e:
            print(f"[sync] could not save sync summary: {e}")
        state.lock.release()
