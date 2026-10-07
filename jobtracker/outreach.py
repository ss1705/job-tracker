"""Outreach tracking: people *you* reached out to (LinkedIn invites / messages / InMails) and whether, and when,
they responded. Incoming requests and inbound messages from strangers are deliberately ignored.

Used by the email watcher (sync), the LinkedIn / spreadsheet importers, and the contacts API.
"""
import re
import unicodedata
from datetime import datetime, timezone
from urllib.parse import unquote

from . import config

_STAGE_RANK = {s: i for i, s in enumerate(config.CONTACT_STAGES)}


def norm_name(name: str) -> str:
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\b(dr|mr|mrs|ms|prof)\b\.?", " ", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def slug_from_url(url: str) -> str:
    m = re.search(r"linkedin\.com/(?:comm/)?in/([^/?#\s\"'<>]+)", url or "", re.I)
    return unquote(m.group(1)).lower().rstrip("/") if m else ""


def display_name(name: str, slug: str = "") -> str:
    """LinkedIn emails only give a first name ("Anurag"); the profile slug has the rest ("anurag-shete-053294178")."""
    if " " in name.strip() or not slug:
        return name.strip()
    words = [w for w in slug.split("-") if w.isalpha()]          # drops the trailing id token(s)
    if len(words) >= 2 and words[0] == norm_name(name).split(" ")[0]:
        return " ".join(w.capitalize() for w in words[:3])
    return name.strip()


def find_contact(conn, name: str, slug: str = ""):
    """By profile slug first; else by full name. A bare first name only matches when that's unambiguous."""
    rows = conn.execute("SELECT * FROM contacts").fetchall()
    if slug:
        for r in rows:
            if r["ext_key"] == slug:
                return r
    full = norm_name(display_name(name, slug))
    if full and " " in full:
        for r in rows:
            if norm_name(r["name"]) == full and not (slug and r["ext_key"] and r["ext_key"] != slug):
                return r
        # a contact saved earlier under just the first name (no profile link) is this person, if unambiguous
        first = full.split(" ")[0]
        bare = [r for r in rows if norm_name(r["name"]) == first and not r["ext_key"]]
        return bare[0] if len(bare) == 1 else None
    if full and not slug:                                          # first name only, no profile link
        same = [r for r in rows if norm_name(r["name"]).split(" ")[:1] == [full]]
        return same[0] if len(same) == 1 else None
    return None


def _earlier(a: str | None, b: str | None) -> str | None:
    return min((x for x in (a, b) if x), default=None)


def _later(a: str | None, b: str | None) -> str | None:
    return max((x for x in (a, b) if x), default=None)


def record_outreach(conn, *, name: str, slug: str = "", sent_at: str | None = None, channel: str = "LinkedIn",
                    notes: str = "", company: str = "", title: str = "", link: str = "", source: str = "manual",
                    kind: str = "Cold outreach") -> tuple[int, bool]:
    """You reached out to this person. Creates or enriches the contact; never downgrades a stage. Returns (id, created)."""
    sent_at = sent_at[:10] if sent_at else None
    c = find_contact(conn, name, slug)
    if c is None:
        cur = conn.execute(
            """INSERT INTO contacts (name, company, title, kind, stage, link, notes, channel, source, ext_key, outreach_sent_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (display_name(name, slug), company, title, kind, "Reached out", link, notes, channel, source, slug, sent_at))
        return cur.lastrowid, True
    conn.execute(
        """UPDATE contacts SET
             company = CASE WHEN company='' THEN ? ELSE company END,
             title = CASE WHEN title='' THEN ? ELSE title END,
             link = CASE WHEN link='' THEN ? ELSE link END,
             notes = CASE WHEN notes='' THEN ? ELSE notes END,
             channel = CASE WHEN channel='' THEN ? ELSE channel END,
             ext_key = CASE WHEN ext_key='' THEN ? ELSE ext_key END,
             outreach_sent_at = ?,
             stage = CASE WHEN stage='To reach out' THEN 'Reached out' ELSE stage END
           WHERE id=?""",
        (company, title, link, notes, channel, slug, _earlier(c["outreach_sent_at"], sent_at), c["id"]))
    return c["id"], False


def record_response(conn, *, name: str, slug: str = "", when: str | None = None, kind: str = "Replied",
                    create_missing: bool = False, company: str = "", title: str = "", link: str = "",
                    source: str = "linkedin-email") -> tuple[int | None, str]:
    """They responded (accepted your invite / replied). Returns (contact id | None, 'created'|'updated'|'unchanged'|'ignored').
    Unknown people are only added when create_missing is true (an accepted invite can only come from someone you invited)."""
    when = (when or datetime.now(timezone.utc).strftime("%Y-%m-%d"))[:10]
    c = find_contact(conn, name, slug)
    # only responses from the last few days count as "new" for the digest; backfilled history must not flood it
    fresh = (datetime.now(timezone.utc).date() - datetime.strptime(when, "%Y-%m-%d").date()).days <= 3
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if fresh else None
    if c is None:
        if not create_missing:
            return None, "ignored"
        cur = conn.execute(
            """INSERT INTO contacts (name, company, title, kind, stage, link, channel, source, ext_key,
                                     responded_at, response_kind, response_logged_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (display_name(name, slug), company, title, "Cold outreach", "Replied", link, "LinkedIn", source, slug, when, kind, now))
        return cur.lastrowid, "created"
    better = display_name(name, slug)
    upgrade = better if (" " in better and " " not in c["name"] and norm_name(c["name"]) == norm_name(better).split(" ")[0]) else c["name"]
    if c["responded_at"] and c["responded_at"] >= when:
        conn.execute("""UPDATE contacts SET name=?, ext_key = CASE WHEN ext_key='' THEN ? ELSE ext_key END,
                          company = CASE WHEN company='' THEN ? ELSE company END,
                          title = CASE WHEN title='' THEN ? ELSE title END,
                          link = CASE WHEN link='' THEN ? ELSE link END WHERE id=?""",
                     (upgrade, slug, company, title, link, c["id"]))
        return c["id"], "unchanged"           # already recorded (e.g. the import saw it first); still enrich blanks
    stage = "Replied" if _STAGE_RANK[c["stage"]] <= _STAGE_RANK["Reached out"] else c["stage"]
    conn.execute(
        """UPDATE contacts SET responded_at=?, response_kind=?, response_logged_at=?, stage=?, name=?,
             ext_key = CASE WHEN ext_key='' THEN ? ELSE ext_key END,
             company = CASE WHEN company='' THEN ? ELSE company END,
             title = CASE WHEN title='' THEN ? ELSE title END,
             link = CASE WHEN link='' THEN ? ELSE link END
           WHERE id=?""",
        (when, kind, now, stage, upgrade, slug, company, title, link, c["id"]))
    return c["id"], "updated"


# ---------------------------------------------------------------- LinkedIn notification emails

_ACCEPTED = re.compile(r"^(?P<n>.+?) accepted your invitation", re.I)
_MESSAGED = re.compile(r"^(?P<n>.+?) (?:just messaged you|sent you a message|replied to your message)", re.I)


def pick_slug(name: str, links: list[str]) -> str:
    """The email links several profiles (suggestions etc.); the person's own starts with their first name."""
    first = norm_name(name).split(" ")[0] if norm_name(name) else ""
    slugs = [slug_from_url(u) for u in links]
    for s in slugs:
        if s and first and s.replace("-", " ").startswith(first):
            return s
    return ""


_CARD_NAME = re.compile(r"See (?P<n>.+?)[’']s connections, experience, and more", re.I)


def parse_profile_card(html_text: str, first_name: str) -> dict:
    """Full name + headline from a LinkedIn notice's HTML text ('See Bharath Prakash’s connections…', then the
    name line, then the headline). Returns {} when the layout isn't recognised."""
    text = re.sub(r"https?://\S+", "", html_text or "")
    m = _CARD_NAME.search(text)
    if not m:
        return {}
    full = " ".join(m.group("n").split())
    if not full.lower().startswith(first_name.lower().split(" ")[0]) or len(full) > 60:
        return {}
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    headline = ""
    for i, l in enumerate(lines):
        if l == full and i + 1 < len(lines):
            nxt = lines[i + 1]
            if nxt.lower() not in ("message", "connect", "follow") and "accepted your invitation" not in nxt.lower():
                headline = nxt[:140]
            break
    company = ""        # headlines mix employers and schools ("MS CS @ USC | SWE"), so a guess is worse than blank
    return {"full_name": full, "headline": headline, "company": company}


def parse_linkedin_email(msg: dict) -> dict | None:
    """A response to *your* outreach, from a LinkedIn notification email; None for everything else."""
    sender = msg.get("sender", "").lower()
    if "linkedin.com" not in sender:
        return None
    subject = " ".join(msg.get("subject", "").split())
    m = _ACCEPTED.match(subject)
    if m:
        kind = "Accepted invite"
    else:
        m = _MESSAGED.match(subject)
        kind = "Replied"
    if not m:
        return None
    name = re.sub(r"\s+", " ", m.group("n")).strip(" .,:;\"'")
    if not name or len(name) > 60 or "@" in name:
        return None
    card = parse_profile_card(msg.get("html_text", ""), name)
    return {"name": name, "kind": kind, "slug": pick_slug(name, msg.get("links", [])),
            "full_name": card.get("full_name", ""), "title": card.get("headline", ""), "company": card.get("company", ""),
            "link": next((u for u in msg.get("links", []) if slug_from_url(u) and slug_from_url(u) == pick_slug(name, msg.get("links", []))), ""),
            "received_at": msg.get("received_at", "")}
