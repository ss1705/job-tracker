"""Deadlines and interview times pulled from OA / interview emails (and added by hand).

The LLM proposes dates; code verifies them: the quoted sentence must really appear in the email, relative phrases
("within 7 days") are computed from the email's own date, and implausible dates are dropped.
"""
import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import config, llm

KINDS = ["OA due", "Interview", "Call", "Other"]

_ZONES = {"PT": "America/Los_Angeles", "PST": "America/Los_Angeles", "PDT": "America/Los_Angeles",
          "ET": "America/New_York", "EST": "America/New_York", "EDT": "America/New_York",
          "CT": "America/Chicago", "CST": "America/Chicago", "CDT": "America/Chicago",
          "MT": "America/Denver", "MST": "America/Denver", "MDT": "America/Denver",
          "UTC": "UTC", "GMT": "UTC", "IST": "Asia/Kolkata", "BST": "Europe/London", "CET": "Europe/Paris", "CEST": "Europe/Paris"}

_HAS_DATE_HINT = re.compile(
    r"\b(\d{1,2}[/-]\d{1,2}([/-]\d{2,4})?|jan(uary)?|feb(ruary)?|mar(ch)?|apr(il)?|may|june?|july?|aug(ust)?|sep(t(ember)?)?|oct(ober)?|"
    r"nov(ember)?|dec(ember)?|\d+\s*(business\s+)?(hour|day|week)s?|deadline|expires?|expiry|valid\s+(until|through|for)|due|no later than|"
    r"(mon|tues|wednes|thurs|fri|satur|sun)day|tomorrow|today|tonight|next week)\b", re.I)


def local_tz() -> ZoneInfo:
    return ZoneInfo(config.TIMEZONE) if config.TIMEZONE else datetime.now().astimezone().tzinfo


def _local_date_of(received_utc: str) -> date:
    dt = datetime.fromisoformat(received_utc).replace(tzinfo=timezone.utc)
    return dt.astimezone(local_tz()).date()


def _norm(text: str) -> str:
    return re.sub(r"[\s\u00a0]+", " ", re.sub(r"[“”\"']", "", text or "")).strip().lower()


def to_local(when: str, tz_abbr: str) -> str | None:
    """'2026-10-12T23:59' (+ 'PT') -> the same moment in your timezone, as 'YYYY-MM-DDTHH:MM'. Date-only stays as is."""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", when):
        return when
    try:
        naive = datetime.fromisoformat(when)
    except ValueError:
        return None
    zone = _ZONES.get((tz_abbr or "").strip().upper().replace(".", ""))
    if zone:
        naive = naive.replace(tzinfo=ZoneInfo(zone)).astimezone(local_tz()).replace(tzinfo=None)
    return naive.strftime("%Y-%m-%dT%H:%M")


_PROMPT = """Extract dated actions from this recruiting email: assessment deadlines and scheduled interviews or calls.

The email was received on {received} (the sender's local date). Subject: {subject}

EMAIL:
{body}

RULES
- Only dates the email states explicitly, or "within N days/hours of this email" (give within_days / within_hours, do not compute it).
- For a weekday without a date ("Tuesday at 2 PM"), give the next such weekday after {received}.
- "kind": "OA due" for a deadline to complete an assessment/challenge/survey/form; "Interview" for a scheduled interview;
  "Call" for a scheduled recruiter or phone call; "Other" otherwise.
- "when": "YYYY-MM-DD" if no time is given, else "YYYY-MM-DDTHH:MM" (24-hour) exactly as the email states it, with "tz" the zone
  abbreviation the email gives (PT, ET, UTC, IST...), or "" if none is given.
- "quote": the exact words from the email that state it (under 200 characters).
- Do not invent anything. Marketing dates, application-opening dates and dates in the past do not count. If nothing qualifies, return an empty list.

Return ONLY JSON: {{"items": [{{"kind": "...", "when": "...", "tz": "...", "within_days": null, "within_hours": null, "quote": "..."}}]}}"""


def extract(subject: str, body: str, received_utc: str) -> list[dict]:
    """Verified milestones from one email: [{kind, due_at, quote, tz_note}]. Never raises on LLM trouble."""
    if not llm.available() or not _HAS_DATE_HINT.search(f"{subject}\n{body}"):
        return []
    received = _local_date_of(received_utc)
    try:
        out = llm.chat_json(_PROMPT.format(received=received.isoformat(), subject=subject, body=(body or "")[:5000]),
                            temperature=0, effort="low", label="milestones", max_tokens=2500)
    except llm.LLMExhausted:
        return []
    found, haystack = [], _norm(f"{subject} {body}")
    for it in (out or {}).get("items") or []:
        if not isinstance(it, dict) or it.get("kind") not in KINDS:
            continue
        quote = str(it.get("quote") or "")[:300]
        if len(_norm(quote)) < 6 or _norm(quote) not in haystack:
            continue                                                    # the model must point at real words in the email
        due = None
        if it.get("within_days"):
            try:
                due = (received + timedelta(days=int(it["within_days"]))).isoformat()
            except (TypeError, ValueError):
                due = None
        elif it.get("within_hours"):
            try:
                due = (datetime.fromisoformat(received_utc).replace(tzinfo=timezone.utc).astimezone(local_tz())
                       + timedelta(hours=int(it["within_hours"]))).strftime("%Y-%m-%dT%H:%M")
            except (TypeError, ValueError):
                due = None
        elif it.get("when"):
            due = to_local(str(it["when"]).strip(), str(it.get("tz") or ""))
        if not due:
            continue
        d = date.fromisoformat(due[:10])
        if d < received - timedelta(days=1) or d > received + timedelta(days=240):
            continue                                                    # in the past or absurdly far away: don't trust it
        tz_note = f"{str(it.get('when') or '')[11:16]} {str(it.get('tz') or '')}".strip() if it.get("when") and "T" in str(it["when"]) else ""
        found.append({"kind": it["kind"], "due_at": due, "quote": quote, "tz_note": tz_note})
    seen, uniq = set(), []
    for f in found:
        if (f["kind"], f["due_at"]) not in seen:
            seen.add((f["kind"], f["due_at"]))
            uniq.append(f)
    return uniq


def store(conn, app_id: int, items: list[dict], received_utc: str, source: str = "email") -> int:
    """Insert, or update when a later email moves the same kind of milestone (a reschedule). Returns how many changed."""
    changed = 0
    for it in items:
        row = conn.execute("SELECT id, email_received_at, due_at FROM milestones WHERE application_id=? AND kind=? AND done=0 AND source=?",
                           (app_id, it["kind"], source)).fetchone()
        if row and row["due_at"] == it["due_at"]:
            continue
        if row and source == "email" and received_utc >= (row["email_received_at"] or ""):
            conn.execute("UPDATE milestones SET due_at=?, quote=?, tz_note=?, email_received_at=? WHERE id=?",
                         (it["due_at"], it["quote"], it["tz_note"], received_utc, row["id"]))
        elif row and source == "email":
            continue                                                    # an older email than the one we already have
        else:
            conn.execute("INSERT INTO milestones (application_id, kind, due_at, source, quote, tz_note, email_received_at) VALUES (?,?,?,?,?,?,?)",
                         (app_id, it["kind"], it["due_at"], source, it["quote"], it["tz_note"], received_utc))
        changed += 1
    return changed


def describe(due_at: str, today: date) -> dict:
    """{days, when}: 'today 11:59 PM', 'tomorrow', 'Fri Oct 16 2:00 PM', 'overdue by 2 days'."""
    d = date.fromisoformat(due_at[:10])
    days = (d - today).days
    clock = ""
    if "T" in due_at:
        t = datetime.strptime(due_at[11:16], "%H:%M")
        clock = f" {t.hour % 12 or 12}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'}"
    if days < 0:
        return {"days": days, "when": f"overdue by {-days} day{'s' if days < -1 else ''}"}
    word = "today" if days == 0 else "tomorrow" if days == 1 else f"{d:%a %b} {d.day}"
    return {"days": days, "when": word + clock}


def upcoming(conn, today: date, horizon_days: int = 60, include_done: bool = False) -> list[dict]:
    rows = conn.execute(
        """SELECT m.*, a.company, a.role FROM milestones m JOIN applications a ON a.id = m.application_id
           WHERE (? OR m.done=0) ORDER BY m.due_at""", (int(include_done),)).fetchall()
    out = []
    for r in rows:
        info = describe(r["due_at"], today)
        if info["days"] > horizon_days or info["days"] < -3:            # drop what's long past or far away
            continue
        out.append({**dict(r), **info})
    return out


def ics(m: dict) -> str:
    """A calendar file for one milestone, with reminders a day and two hours before."""
    uid = f"milestone-{m['id']}@jobtracker.local"
    title = f"{m['kind']}: {m['company']}" + (f" ({m['role']})" if m.get("role") else "")
    esc = lambda t: str(t).replace("\\", "\\\\").replace(";", "\;").replace(",", "\\,").replace("\n", "\\n")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if "T" in m["due_at"]:
        start = datetime.strptime(m["due_at"], "%Y-%m-%dT%H:%M")
        end = start + timedelta(minutes=30 if m["kind"] != "Interview" else 60)
        when = f"DTSTART:{start:%Y%m%dT%H%M%S}\r\nDTEND:{end:%Y%m%dT%H%M%S}"
    else:
        d = date.fromisoformat(m["due_at"])
        when = f"DTSTART;VALUE=DATE:{d:%Y%m%d}\r\nDTEND;VALUE=DATE:{d + timedelta(days=1):%Y%m%d}"
    alarms = "".join(f"BEGIN:VALARM\r\nACTION:DISPLAY\r\nDESCRIPTION:{esc(title)}\r\nTRIGGER:-{t}\r\nEND:VALARM\r\n" for t in ("P1D", "PT2H"))
    return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//jobtracker//EN\r\nBEGIN:VEVENT\r\n"
            f"UID:{uid}\r\nDTSTAMP:{stamp}\r\n{when}\r\nSUMMARY:{esc(title)}\r\n"
            f"DESCRIPTION:{esc(m.get('quote') or m.get('note') or '')}\r\n{alarms}END:VEVENT\r\nEND:VCALENDAR\r\n")
