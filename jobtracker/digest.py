"""The daily digest: what needs attention today, as a short email to yourself.

Sections (each omitted when empty; nothing at all to say = no email unless DIGEST_SEND_EMPTY):
  * Responses (new since yesterday): OA / interview / offer / rejection emails and LinkedIn replies
  * People to follow up with (contacts)
  * Applications to follow up (those with a referral / coffee chat / recruiter)
  * Saved postings due within 3 days (or overdue)
"""
import html as _html
import json
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import config

RESPONSE_LABEL = {"OA": "Online assessment", "Interview": "Interview", "Offer": "Offer", "Rejected": "Rejection"}


def recent_events(conn, hours: int = 30) -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")
    rows = conn.execute(
        """SELECT a.company, a.role, e.type, e.received_at FROM events e JOIN applications a ON a.id = e.application_id
           WHERE e.received_at >= ? AND e.type IN ('OA','Interview','Offer','Rejected') ORDER BY e.received_at""", (since,)).fetchall()
    return [dict(r) for r in rows]


def recent_responses(conn, hours: int = 30) -> list[dict]:
    """LinkedIn (and other) outreach responses recorded recently."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        rows = conn.execute(
            "SELECT name, company, response_kind, responded_at FROM contacts WHERE response_logged_at >= ? AND needs_triage=0 ORDER BY response_logged_at",
            (since,)).fetchall()
    except Exception:
        return []     # contacts outreach columns not present yet
    return [dict(r) for r in rows]


def build(apps: list[dict], people: list[dict], events: list[dict], responses: list[dict], today: date,
          upcoming: list[dict] | None = None) -> dict | None:
    sections: list[tuple[str, list[str]]] = []

    soon = sorted((m for m in (upcoming or []) if not m.get("done") and -1 <= m["days"] <= 3), key=lambda m: m["due_at"])
    if soon:
        sections.append(("Coming up", [f"{m['kind']}: {m['company']}" + (f" ({m['role']})" if m.get("role") else "") + f" — {m['when']}" for m in soon]))


    lines = [f"{RESPONSE_LABEL[e['type']]}: {e['company']}" + (f" ({e['role']})" if e["role"] else "") for e in events]
    lines += [f"{r['name']}" + (f" ({r['company']})" if r["company"] else "") + f" {r['response_kind'].lower()} on LinkedIn" for r in responses]
    if lines:
        sections.append(("New responses", lines))

    to_sort = [p for p in people if p.get("needs_triage")]
    if to_sort:
        sections.append(("LinkedIn people to sort", [f"{len(to_sort)} new LinkedIn contact{'s' if len(to_sort) != 1 else ''} to keep or ignore (open the Contacts tab)"]))

    due_people = sorted((p for p in people if p["follow_up_due"]), key=lambda p: p["name"])
    if due_people:
        sections.append(("People to follow up with", [
            f"{p['name']}" + (f", {p['company']}" if p["company"] else "") + f": {p['follow_up_reason']}" for p in due_people]))

    due_apps = [a for a in apps if a["follow_up_due"]]
    if due_apps:
        sections.append(("Applications to follow up", [
            f"{a['company']}" + (f" ({a['role']})" if a["role"] else "") + f": {a['follow_up_reason']}"
            + (f" via {a['contact_name'] or a['contact_type']}" if a.get("contact_type") else "") for a in due_apps]))

    deadlines = sorted((a for a in apps if a["deadline_days"] is not None and a["deadline_days"] <= 3), key=lambda a: a["deadline_days"])
    if deadlines:
        def when(d):
            return f"overdue by {-d} day{'s' if d < -1 else ''}" if d < 0 else "due today" if d == 0 else f"due in {d} day{'s' if d > 1 else ''}"
        sections.append(("Saved postings closing soon", [
            f"{a['company']}" + (f" ({a['role']})" if a["role"] else "") + f": {when(a['deadline_days'])}" for a in deadlines]))

    if not sections:
        return None

    n = sum(len(items) for _, items in sections)
    bits = []
    if soon: bits.insert(0, f"{len(soon)} coming up")
    if lines: bits.append(f"{len(lines)} new response{'s' if len(lines) != 1 else ''}")
    follow = len(due_people) + len(due_apps)
    if follow: bits.append(f"{follow} to follow up")
    if deadlines: bits.append(f"{len(deadlines)} deadline{'s' if len(deadlines) != 1 else ''}")
    subject = f"Job tracker: {', '.join(bits)} ({today:%b} {today.day})"

    text = "\n\n".join(f"{title.upper()}\n" + "\n".join(f"- {i}" for i in items) for title, items in sections)
    h = "".join(
        f"<h3 style='margin:18px 0 6px;font-size:14px;text-transform:uppercase;letter-spacing:.05em;color:#6c6c66'>{_html.escape(title)}</h3>"
        "<ul style='margin:0;padding-left:20px'>" + "".join(f"<li style='margin:4px 0'>{_html.escape(i)}</li>" for i in items) + "</ul>"
        for title, items in sections)
    html = f"<div style='font:15px/1.5 -apple-system,Segoe UI,sans-serif;color:#1b1b19;max-width:560px'>{h}</div>"
    return {"subject": subject, "text": text, "html": html, "count": n}


def collect(today: date | None = None) -> dict | None:
    """Gather current state from the database and build the digest (None = nothing to report)."""
    from . import api, contacts, db, milestones      # imported lazily: api imports the scheduler, which imports this module
    today = today or date.today()
    apps = api.list_applications()
    with db.session() as conn:
        people = contacts.list_with_flags(conn)
        events, responses = recent_events(conn), recent_responses(conn)
        upcoming = milestones.upcoming(conn, today, 7)
    return build(apps, people, events, responses, today, upcoming)


def local_now() -> datetime:
    return datetime.now(ZoneInfo(config.TIMEZONE)) if config.TIMEZONE else datetime.now().astimezone()


def is_due(now_local: datetime, last: dict | None, hour: int, retry_minutes: int = 60) -> bool:
    """Send once per local day, on the first check at/after `hour`; retry a failed send hourly
    (but never retry a missing-permission failure: it can't fix itself)."""
    if now_local.hour < hour:
        return False
    if not last or last.get("date") != now_local.date().isoformat():
        return True
    if last.get("sent") or last.get("skipped") or last.get("needs_permission"):
        return False
    at = datetime.strptime(last["at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - at >= timedelta(minutes=retry_minutes)


def last_digest() -> dict | None:
    from . import db
    with db.session() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key='last_digest'").fetchone()
    return json.loads(row[0]) if row else None


def _record(result: dict):
    from . import db
    with db.session() as conn:
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('last_digest', ?)", (json.dumps(result),))


def send_now(test: bool = False) -> dict:
    """Build and email the digest. test=True always sends (a short note if there's nothing to report)."""
    from . import gmail
    now = local_now()
    result = {"date": now.date().isoformat(), "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "sent": False, "skipped": False, "error": "", "needs_permission": False, "count": 0, "test": test}
    try:
        d = collect(now.date())
        if d is None:
            if not test and not config.DIGEST_SEND_EMPTY:
                result["skipped"] = True
                return result
            d = {"subject": f"Job tracker: nothing needs attention ({now:%b} {now.day})", "count": 0,
                 "text": "Nothing to follow up on today.", "html": "<p style='font:15px sans-serif'>Nothing to follow up on today.</p>"}
        if test:
            d["subject"] = "[Test] " + d["subject"]
        result["to"] = gmail.send_to_self(d["subject"], d["text"], d["html"])
        result.update(sent=True, count=d["count"])
    except gmail.SendPermissionMissing as e:
        result.update(error=str(e), needs_permission=True)
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
    finally:
        if not test or result["sent"]:
            _record(result)
    return result
