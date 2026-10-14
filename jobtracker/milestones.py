"""Deadlines and interview times pulled from OA / interview emails (and added by hand).

The LLM proposes dates; code verifies them: the quoted sentence must really appear in the email, relative phrases
("within 7 days") are computed from the email's own date, and implausible dates are dropped.
"""
import html
import re
import unicodedata
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import config, llm

KINDS = ["OA due", "Action due", "Interview", "Call", "Other"]

_ZONES = {"PT": "America/Los_Angeles", "PST": "America/Los_Angeles", "PDT": "America/Los_Angeles",
          "ET": "America/New_York", "EST": "America/New_York", "EDT": "America/New_York",
          "CT": "America/Chicago", "CST": "America/Chicago", "CDT": "America/Chicago",
          "MT": "America/Denver", "MST": "America/Denver", "MDT": "America/Denver",
          "UTC": "UTC", "GMT": "UTC", "IST": "Asia/Kolkata", "BST": "Europe/London", "CET": "Europe/Paris", "CEST": "Europe/Paris"}

_HAS_DATE_HINT = re.compile(
    r"\b(\d{1,2}[/-]\d{1,2}([/-]\d{2,4})?|jan(uary)?|feb(ruary)?|mar(ch)?|apr(il)?|may|june?|july?|aug(ust)?|sep(t(ember)?)?|oct(ober)?|"
    r"nov(ember)?|dec(ember)?|(\d+|\(\d+\)|one|two|three|four|five|six|seven|eight|nine|ten|twelve|fourteen|fifteen|twenty|thirty)\s*(business\s+)?(hour|day|week)s?|"
    r"deadline|expires?|expiry|valid\s+(until|through|for)|due|no later than|"
    r"(mon|tues|wednes|thurs|fri|satur|sun)day|tomorrow|today|tonight|next week)\b", re.I)


def local_tz() -> ZoneInfo:
    return ZoneInfo(config.TIMEZONE) if config.TIMEZONE else datetime.now().astimezone().tzinfo


def _local_date_of(received_utc: str) -> date:
    dt = datetime.fromisoformat(received_utc).replace(tzinfo=timezone.utc)
    return dt.astimezone(local_tz()).date()


def _fold(text: str) -> str:
    """Letters and digits only, after decoding HTML entities and Unicode look-alikes. Used to check that the model's quote
    really is in the email without tripping on curly quotes, dashes, line breaks or a leftover '&nbsp;'."""
    s = unicodedata.normalize("NFKC", html.unescape(text or ""))
    return re.sub(r"[^a-z0-9]+", "", s.lower())


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
- "kind": "OA due" for a deadline to complete an assessment or coding challenge; "Action due" for a deadline to submit requested
  information, a form, documents or a verification; "Interview" for a scheduled interview;
  "Call" for a scheduled recruiter or phone call; "Other" otherwise.
- "when": "YYYY-MM-DD" if no time is given, else "YYYY-MM-DDTHH:MM" (24-hour) exactly as the email states it, with "tz" the zone
  abbreviation the email gives (PT, ET, UTC, IST...), or "" if none is given.
- "quote": the exact words from the email that state it (under 200 characters).
- Do not invent anything. Marketing dates, application-opening dates and dates in the past do not count. If nothing qualifies, return an empty list.

Return ONLY JSON: {{"items": [{{"kind": "...", "when": "...", "tz": "...", "within_days": null, "within_hours": null, "quote": "..."}}]}}"""


_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
          "twelve": 12, "fourteen": 14, "fifteen": 15, "twenty": 20, "thirty": 30}
_REL = re.compile(r"\b(?:within|in|for|have|has|up to|over)\s+(?:the\s+next\s+)?(?P<n>\d{1,3}|" + "|".join(_WORDS) + r")\s*(?:\(\d{1,3}\)\s*)?"
                  r"(?P<biz>business\s+|working\s+)?(?P<unit>hour|day|week)s?\b", re.I)
_USER_ACTION = re.compile(r"complet|finish|submit|take the|attempt|respond|reply|return|upload|valid|expire|must be done|available for", re.I)
_NOT_A_DEADLINE = re.compile(r"get back to you|contact you|hear from|be in touch|notify you|review your|let you know|results? (will|within)|"
                             r"we will (respond|reply|review)|\bago\b|last \d|past \d|previous \d", re.I)


def _add_business_days(start: date, n: int) -> date:
    d = start
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def rule_deadlines(text: str, received_utc: str) -> list[dict]:
    """The common case without any model: a sentence that tells YOU to do something within N hours/days/weeks of the email."""
    received = _local_date_of(received_utc)
    out = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", html.unescape(text or "")):
        sentence = " ".join(sentence.split())
        m = _REL.search(sentence)
        if not m or not _USER_ACTION.search(sentence) or _NOT_A_DEADLINE.search(sentence) or len(sentence) > 300:
            continue
        raw = m.group("n").lower()
        n = int(raw) if raw.isdigit() else _WORDS[raw]
        unit = m.group("unit").lower()
        if unit == "hour":
            due = (datetime.fromisoformat(received_utc).replace(tzinfo=timezone.utc).astimezone(local_tz())
                   + timedelta(hours=n)).strftime("%Y-%m-%dT%H:%M")
        elif m.group("biz") and unit == "day":
            due = _add_business_days(received, n).isoformat()
        else:
            due = (received + timedelta(days=n * (7 if unit == "week" else 1))).isoformat()
        kind = "OA due" if re.search(r"assessment|test|challenge|coding|hackerrank|codesignal|codility|quiz", sentence, re.I) else \
               "Action due" if re.search(r"form|document|upload|verif|confirm|information|profile|application|submit", sentence, re.I) else "Other"
        out.append({"kind": kind, "due_at": due, "quote": sentence[:300], "tz_note": ""})
    return out


def extract(subject: str, body: str, received_utc: str, html_text: str = "") -> list[dict]:
    """Verified milestones from one email: [{kind, due_at, quote, tz_note}]. Never raises on LLM trouble."""
    return extract_detailed(subject, body, received_utc, html_text)["items"]


def extract_detailed(subject: str, body: str, received_utc: str, html_text: str = "") -> dict:
    """Like extract, but says what happened: {items, outcome, detail}. Outcomes:
    found | no_dates_found (the model saw none) | no_date_wording (nothing date-like in the email) |
    rejected (the model proposed dates that failed verification) | llm_failed (retry later) | no_llm (no API key).
    A rule for "within N days" runs first and always counts, so a model outage can't lose that common deadline."""
    body = html.unescape(body or "")
    html_text = html.unescape(html_text or "")
    if len(html_text) > 1.3 * len(body):          # some senders' plain-text part is only a footer; the real message is in the HTML
        body = html_text
    by_rule = rule_deadlines(f"{subject}\n{body}", received_utc)
    model = _model_items(subject, body, received_utc)
    merged, seen = [], set()
    for it in [*model["items"], *by_rule]:         # the model's richer items first; the rule fills in what it missed
        if (it["kind"], it["due_at"]) not in seen:
            seen.add((it["kind"], it["due_at"]))
            merged.append(it)
    if merged:
        how = "model + rule" if model["items"] and by_rule else "model" if model["items"] else "rule"
        return {"items": merged, "outcome": "found", "detail": f"{len(merged)} found ({how})"}
    return {"items": [], "outcome": model["outcome"], "detail": model["detail"]}


def _model_items(subject: str, body: str, received_utc: str) -> dict:
    if not llm.available():
        return {"items": [], "outcome": "no_llm", "detail": "No GROQ_API_KEY configured"}
    if not _HAS_DATE_HINT.search(f"{subject}\n{body}"):
        return {"items": [], "outcome": "no_date_wording", "detail": "No date or time-limit wording in the email"}
    received = _local_date_of(received_utc)
    try:
        out = llm.chat_json(_PROMPT.format(received=received.isoformat(), subject=subject, body=body[:5000]),
                            temperature=0, effort="low", label="milestones", max_tokens=2500)
    except llm.LLMExhausted as e:
        return {"items": [], "outcome": "llm_failed", "detail": f"Daily model limit reached ({e})"[:200]}
    if out is None:
        return {"items": [], "outcome": "llm_failed", "detail": "The language model did not answer"}
    proposed = [it for it in (out.get("items") or []) if isinstance(it, dict)]
    if not proposed:
        return {"items": [], "outcome": "no_dates_found", "detail": "The email states no deadline or scheduled time"}
    found, haystack, reasons = [], _fold(f"{subject} {body}"), []
    for it in proposed:
        if it.get("kind") not in KINDS:
            reasons.append("unknown kind")
            continue
        quote = str(it.get("quote") or "")[:300]
        if len(_fold(quote)) < 10 or _fold(quote) not in haystack:
            reasons.append("quote not found in the email")          # the model must point at real words in the email
            continue
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
            reasons.append("date could not be read")
            continue
        d = date.fromisoformat(due[:10])
        if d < received - timedelta(days=1) or d > received + timedelta(days=240):
            reasons.append("date out of range")                     # in the past or absurdly far away: don't trust it
            continue
        tz_note = f"{str(it.get('when') or '')[11:16]} {str(it.get('tz') or '')}".strip() if it.get("when") and "T" in str(it["when"]) else ""
        found.append({"kind": it["kind"], "due_at": due, "quote": quote, "tz_note": tz_note})
    seen, uniq = set(), []
    for f in found:
        if (f["kind"], f["due_at"]) not in seen:
            seen.add((f["kind"], f["due_at"]))
            uniq.append(f)
    if uniq:
        return {"items": uniq, "outcome": "found", "detail": f"{len(uniq)} found"}
    return {"items": [], "outcome": "rejected", "detail": "; ".join(sorted(set(reasons)))[:200] or "nothing usable"}


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
