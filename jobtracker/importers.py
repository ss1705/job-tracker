"""Bring existing outreach history in: LinkedIn's data export and an old contacts spreadsheet (CSV).

Both build a *plan* first (nothing is written), so the UI can preview; `apply_plan` then writes it through
outreach.record_* so re-importing is idempotent. Only outreach YOU initiated is imported:
  * Invitations.csv rows with Direction OUTGOING (an accepted one is recognised via Connections.csv)
  * messages.csv conversations whose first message is from you (a later message from them = a reply)
Incoming invitations and conversations someone else started are skipped.
"""
import csv
import io
import re
from collections import Counter, defaultdict
from datetime import date, datetime

from . import config, outreach


# ---------------------------------------------------------------- helpers

def _rows_from_header(text: str, first_col: str) -> list[dict]:
    """LinkedIn CSVs sometimes start with a few lines of notes; find the real header row."""
    lines = text.lstrip("﻿").splitlines()
    for i, line in enumerate(lines):
        if line.strip().lower().startswith(first_col.lower()):
            return list(csv.DictReader(io.StringIO("\n".join(lines[i:]))))
    return []


def parse_date(s: str) -> str | None:
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S UTC", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%m/%d/%y, %I:%M %p", "%m/%d/%Y, %I:%M %p",
                "%d %b %Y", "%b %d, %Y", "%B %d, %Y", "%m/%d/%Y", "%m/%d/%y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    return m.group(1) if m else None


def _get(row: dict, *names: str) -> str:
    low = {k.strip().lower(): (v or "") for k, v in row.items() if k}
    for n in names:
        if n.lower() in low and low[n.lower()].strip():
            return low[n.lower()].strip()
    return ""


# ---------------------------------------------------------------- LinkedIn export

def plan_linkedin(files: dict[str, str], since: str | None = None, me: str | None = None) -> dict:
    by_name = {k.lower().rsplit("/", 1)[-1]: v for k, v in files.items()}
    people: dict[str, dict] = {}          # key: slug or normalised name

    def person(name: str, slug: str) -> dict:
        key = slug or outreach.norm_name(name)
        p = people.get(key)
        if p is None:
            p = people[key] = {"name": name.strip(), "slug": slug, "company": "", "title": "", "sent_at": None,
                               "responded_at": None, "kind": "", "notes": "", "link": f"https://www.linkedin.com/in/{slug}" if slug else ""}
        return p

    connections = {}
    for r in _rows_from_header(by_name.get("connections.csv", ""), "First Name"):
        slug = outreach.slug_from_url(_get(r, "URL"))
        name = f"{_get(r, 'First Name')} {_get(r, 'Last Name')}".strip()
        if slug or name:
            connections[slug or outreach.norm_name(name)] = {"company": _get(r, "Company"), "title": _get(r, "Position"),
                                                             "on": parse_date(_get(r, "Connected On")), "name": name}

    skipped_incoming = 0
    for r in _rows_from_header(by_name.get("invitations.csv", ""), "From"):
        if _get(r, "Direction").upper() != "OUTGOING":
            skipped_incoming += 1
            continue
        sent = parse_date(_get(r, "Sent At"))
        if since and sent and sent < since:
            continue
        slug = outreach.slug_from_url(_get(r, "inviteeProfileUrl"))
        p = person(_get(r, "To"), slug)
        p["sent_at"] = outreach._earlier(p["sent_at"], sent)
        p["notes"] = p["notes"] or _get(r, "Message")
        conn_ = connections.get(slug) or connections.get(outreach.norm_name(p["name"]))
        if conn_ and conn_["on"] and (not sent or conn_["on"] >= sent):
            p["responded_at"], p["kind"] = conn_["on"], "Accepted invite"

    msgs = _rows_from_header(by_name.get("messages.csv", ""), "CONVERSATION ID")
    if msgs:
        mine = me or Counter(_get(r, "FROM") for r in msgs if _get(r, "FROM")).most_common(1)[0][0]
        convs = defaultdict(list)
        for r in msgs:
            convs[_get(r, "CONVERSATION ID")].append(r)
        for rows in convs.values():
            rows.sort(key=lambda r: parse_date(_get(r, "DATE")) or "")
            senders = {_get(r, "FROM") for r in rows}
            if len(senders - {mine}) > 1 or _get(rows[0], "FROM") != mine:
                continue                                    # group chat, or they started it: not your outreach
            first = rows[0]
            sent = parse_date(_get(first, "DATE"))
            if since and sent and sent < since:
                continue
            theirs = [r for r in rows if _get(r, "FROM") != mine]
            other_name = _get(theirs[0], "FROM") if theirs else _get(first, "TO")
            other_url = _get(theirs[0], "SENDER PROFILE URL") if theirs else _get(first, "RECIPIENT PROFILE URLS")
            if not other_name or "," in other_name:
                continue
            p = person(other_name, outreach.slug_from_url(other_url))
            p["sent_at"] = outreach._earlier(p["sent_at"], sent)
            if theirs:
                last_reply = parse_date(_get(theirs[-1], "DATE"))
                if not p["responded_at"] or (last_reply and last_reply > p["responded_at"]):
                    p["responded_at"], p["kind"] = last_reply, "Replied"

    for key, p in people.items():              # enrich with company / title from Connections.csv
        c = connections.get(p["slug"]) or connections.get(outreach.norm_name(p["name"]))
        if c:
            p["company"], p["title"] = p["company"] or c["company"], p["title"] or c["title"]
    return {"kind": "linkedin", "people": list(people.values()), "skipped_incoming_invites": skipped_incoming,
            "files_found": sorted(by_name)}


# ---------------------------------------------------------------- spreadsheet

_SYNONYMS = {
    "name": ("name", "full name", "contact", "person", "contact name"),
    "company": ("company", "organization", "organisation", "employer", "org", "firm"),
    "title": ("title", "role title", "position", "job title", "their title", "role"),
    "link": ("linkedin url", "linkedin", "linkedin profile", "profile", "profile link", "url", "link"),
    "kind": ("kind", "relationship", "category", "contact type"),
    "date": ("date first contacted", "date contacted", "first contacted", "date", "contacted", "reached out",
             "outreach date", "date reached out", "sent"),
    "last_touch": ("date last touch", "last touch", "last contacted", "last contact", "date last contacted", "last touchpoint"),
    "status": ("status", "stage", "response", "reply", "replied", "outcome"),
    "notes": ("notes what they said", "notes", "note", "comments", "comment", "details"),
    "referral": ("referral given", "referred", "referral"),
}


def _norm_header(h: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", (h or "").lower())).strip()


def detect_mapping(headers: list[str]) -> dict:
    """field -> original header, matching on normalised names ("Notes / what they said" -> "notes what they said").
    Each header is used for at most one field."""
    norm = {_norm_header(h): h for h in headers if h}
    out, used = {}, set()
    for field, names in _SYNONYMS.items():
        for n in names:
            if n in norm and norm[n] not in used:
                out[field] = norm[n]
                used.add(norm[n])
                break
    if "name" not in out and "first name" in norm:
        out["name"] = norm["first name"]
    return out


def _stage_from_status(text: str, has_date: bool) -> str:
    t = text.lower()
    if re.search(r"not (yet )?(contacted|messaged|reached)|to contact|to do|todo|planned|queued|haven.?t|not started|^no$", t):
        return "To reach out"
    if re.search(r"refer", t):
        return "Referred"
    if re.search(r"chat(ted)?\b|coffee|call|met\b|spoke|talked|zoom", t) and not re.search(r"schedul|upcoming|booked", t):
        return "Chatted"
    if re.search(r"schedul|upcoming|booked", t):
        return "Chat scheduled"
    if re.search(r"repl|respond|accept|connected|yes", t) and not re.search(r"no repl|no resp|not repl", t):
        return "Replied"
    if re.search(r"close|decline|not interested|dead", t):
        return "Closed"
    return "Reached out" if (has_date or t) else "To reach out"


def plan_sheet(text: str, mapping: dict | None = None) -> dict:
    text = text.lstrip("\ufeff")
    rows = list(csv.DictReader(io.StringIO(text)))
    headers = list(rows[0].keys()) if rows else []
    m = {**detect_mapping(headers), **(mapping or {})}
    mapped = set(m.values()) | {h for h in headers if _norm_header(h) in ("first name", "last name")}
    extras = [h for h in headers if h and h not in mapped]          # columns we don't model: kept as labelled lines in notes

    def cell(r, field):
        return (r.get(m.get(field, ""), "") or "").strip()

    people = []
    for r in rows:
        name = cell(r, "name")
        if not name and "first name" in {_norm_header(h) for h in headers}:
            name = f"{_get(r, 'first name')} {_get(r, 'last name')}".strip()
        if not name:
            continue
        link, sent = cell(r, "link"), parse_date(cell(r, "date"))
        kind = next((k for k in config.CONTACT_KINDS if k.lower() == cell(r, "kind").lower()), "Cold outreach")
        stage = _stage_from_status(cell(r, "status"), bool(sent))
        if re.match(r"(?i)\s*(y|yes|true|x|✓|given|received)", cell(r, "referral")):
            stage = "Referred"
        notes = "\n".join(x for x in [cell(r, "notes")] + [f"{h}: {(r.get(h) or '').strip()}" for h in extras if (r.get(h) or "").strip()] if x)
        people.append({"name": name, "slug": outreach.slug_from_url(link), "company": cell(r, "company"), "title": cell(r, "title"),
                       "sent_at": sent, "last_touch": parse_date(cell(r, "last_touch")), "responded_at": None, "kind": "",
                       "notes": notes, "link": link, "contact_kind": kind, "stage": stage,
                       "channel": "LinkedIn" if "linkedin" in link.lower() else ""})
    return {"kind": "sheet", "people": people, "mapping": m, "headers": headers, "extra_columns": extras}


# ---------------------------------------------------------------- applying a plan

def apply_plan(conn, plan: dict, source: str) -> dict:
    created = updated = responses = 0
    for p in plan["people"]:
        cid, was_created = outreach.record_outreach(
            conn, name=p["name"], slug=p["slug"], sent_at=p["sent_at"], company=p["company"], title=p["title"], link=p["link"],
            notes=p["notes"], source=source, channel=p.get("channel") or ("LinkedIn" if source == "linkedin-import" else ""),
            kind=p.get("contact_kind", "Cold outreach"))
        if not cid:                      # on your ignore list
            continue
        created += was_created
        updated += not was_created
        if p.get("responded_at"):
            _, how = outreach.record_response(conn, name=p["name"], slug=p["slug"], when=p["responded_at"],
                                              kind=p["kind"] or "Replied", create_missing=False, source=source)
            responses += how == "updated"
        if source == "sheet-import":     # being in your own CRM means you've already decided to track them
            conn.execute("UPDATE contacts SET needs_triage=0 WHERE id=?", (cid,))
        if p.get("last_touch"):
            conn.execute("UPDATE contacts SET last_contacted_at = CASE WHEN last_contacted_at IS NULL OR last_contacted_at < ? THEN ? ELSE last_contacted_at END WHERE id=?",
                         (p["last_touch"], p["last_touch"], cid))
        stage = p.get("stage")            # spreadsheet: a new contact takes the sheet's stage as is; an existing one only moves forward
        if stage and was_created:
            conn.execute("UPDATE contacts SET stage=? WHERE id=?", (stage, cid))
        elif stage and stage != "Reached out":
            rank = {s: i for i, s in enumerate(config.CONTACT_STAGES)}
            cur = conn.execute("SELECT stage FROM contacts WHERE id=?", (cid,)).fetchone()[0]
            if stage == "Closed" or (cur != "Closed" and rank[stage] > rank[cur]):
                conn.execute("UPDATE contacts SET stage=? WHERE id=?", (stage, cid))
    return {"created": created, "updated": updated, "responses": responses}


def summarize(conn, plan: dict) -> dict:
    """What applying would do, without doing it."""
    new = existing = replied = 0
    for p in plan["people"]:
        if outreach.find_contact(conn, p["name"], p["slug"]):
            existing += 1
        else:
            new += 1
        replied += bool(p.get("responded_at") or p.get("stage") in ("Replied", "Chatted", "Referred"))
    sample = [{"name": p["name"], "company": p["company"], "sent_at": p["sent_at"],
               "response": (p["kind"] + (f" {p['responded_at']}" if p.get("responded_at") else "")).strip() or p.get("stage", "")}
              for p in plan["people"][:12]]
    extra = {k: plan[k] for k in ("skipped_incoming_invites", "files_found", "mapping", "headers", "extra_columns") if k in plan}
    return {"total": len(plan["people"]), "new": new, "existing": existing, "with_response": replied, "sample": sample, **extra}
