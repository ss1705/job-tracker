"""HTTP endpoints for the resume and outreach drafting."""
import base64
import json
import re
from datetime import date

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import db, drafting, llm, outreach, recruiter, resume

router = APIRouter(prefix="/api", tags=["outreach"])


class ResumeUpload(BaseModel):
    filename: str
    data: str            # base64


class DraftRequest(BaseModel):
    url: str = ""
    name: str = ""
    profile_text: str = ""
    contact_id: int | None = None
    usc_override: str | None = None      # "yes" | "no" | None (auto-detect)
    tone: str = "standard"
    length: str = "standard"
    instructions: str = ""               # free-text style request, e.g. "less formal"
    job_text: str = ""                   # a role you're targeting: picks the resume lines to lean on


class RecruiterRequest(BaseModel):
    application_id: int
    name: str = ""
    url: str = ""
    profile_text: str = ""
    contact_id: int | None = None
    tone: str = "standard"
    length: str = "standard"
    instructions: str = ""
    job_text: str = ""


class FollowupRequest(BaseModel):
    tone: str = "standard"
    instructions: str = ""


class SentRequest(BaseModel):
    which: str           # "note" | "message"
    text: str = ""


@router.get("/resume")
def resume_status():
    return resume.status()


@router.post("/resume")
def upload_resume(body: ResumeUpload):
    try:
        return resume.save(body.filename, base64.b64decode(body.data))
    except resume.ResumeError as e:
        raise HTTPException(422, str(e))
    except Exception:
        raise HTTPException(422, "Couldn't read that upload.")


def _tone(v: str) -> str:
    if v not in recruiter.TONES:
        raise HTTPException(422, f"tone must be one of {list(recruiter.TONES)}")
    return v


def _length(v: str) -> str:
    if v not in recruiter.LENGTHS:
        raise HTTPException(422, f"length must be one of {list(recruiter.LENGTHS)}")
    return v


@router.post("/outreach/draft")
def make_draft(body: DraftRequest):
    if body.usc_override not in (None, "yes", "no"):
        raise HTTPException(422, "usc_override must be yes, no or empty")
    url, slug = drafting.clean_profile_url(body.url)
    if not (url or body.contact_id or body.name.strip()):
        raise HTTPException(422, "Paste a LinkedIn profile link (or at least a name).")
    with db.session() as conn:
        c = conn.execute("SELECT * FROM contacts WHERE id=?", (body.contact_id,)).fetchone() if body.contact_id \
            else outreach.find_contact(conn, body.name or drafting.name_from_slug(slug), slug)
        name = body.name.strip() or (c["name"] if c else drafting.name_from_slug(slug))
        profile = body.profile_text.strip() or (c["profile_text"] if c else "")
        if c and c["title"] and c["title"] not in profile:
            profile = f"{c['title']}\n{profile}".strip()       # a headline we already know (e.g. from a LinkedIn notice)
    try:
        d = drafting.draft(name=name, profile_text=profile, url=url, usc_override=body.usc_override, tone=_tone(body.tone),
                           length=_length(body.length), instructions=body.instructions, job_text=body.job_text)
    except llm.LLMExhausted as e:
        raise HTTPException(429, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))

    rec = d["recipient"] or {}
    full = " ".join(str(rec.get("name") or "").split())
    if full and len(full.split()) >= 2 and full.lower().startswith(name.lower().split(" ")[0]) and (not c or c["name"] == name):
        name = full[:80]                                         # the pasted profile knows the real name better than the URL slug
    with db.session() as conn:
        if c is None:
            cur = conn.execute(
                """INSERT INTO contacts (name, company, title, kind, stage, link, channel, source, ext_key, needs_triage)
                   VALUES (?,?,?,?,?,?,?,?,?,0)""",
                (name, str(rec.get("company") or "")[:80], str(rec.get("role") or "")[:140], "Cold outreach", "To reach out",
                 url, "LinkedIn", "manual", slug))
            cid = cur.lastrowid
        else:
            cid = c["id"]
            conn.execute("""UPDATE contacts SET name=?, link = CASE WHEN link='' THEN ? ELSE link END,
                              ext_key = CASE WHEN ext_key='' THEN ? ELSE ext_key END,
                              company = CASE WHEN company='' THEN ? ELSE company END,
                              title = CASE WHEN title='' THEN ? ELSE title END, needs_triage=0 WHERE id=?""",
                         (name, url, slug, str(rec.get("company") or "")[:80], str(rec.get("role") or "")[:140], cid))
        conn.execute("UPDATE contacts SET profile_text=?, draft_json=? WHERE id=?",
                     (body.profile_text.strip() or (c["profile_text"] if c else ""), json.dumps(d), cid))
        row = conn.execute("SELECT id, name, company, title, stage, link FROM contacts WHERE id=?", (cid,)).fetchone()
    return {"contact": dict(row), "draft": d}


@router.get("/outreach/contact/{contact_id}")
def saved_draft(contact_id: int):
    with db.session() as conn:
        c = conn.execute("SELECT id, name, company, title, stage, link, profile_text, draft_json, application_id, outreach_sent_at FROM contacts WHERE id=?",
                         (contact_id,)).fetchone()
    if not c:
        raise HTTPException(404)
    return {"contact": {k: c[k] for k in ("id", "name", "company", "title", "stage", "link", "application_id", "outreach_sent_at")},
            "profile_text": c["profile_text"], "draft": json.loads(c["draft_json"]) if c["draft_json"] else None}


@router.get("/outreach/application/{app_id}")
def application_for_outreach(app_id: int):
    """What the recruiter form needs: the application, the job description saved for it, and any recruiter contact already linked."""
    with db.session() as conn:
        a = conn.execute("SELECT id, company, role, status, applied_at FROM applications WHERE id=?", (app_id,)).fetchone()
        if not a:
            raise HTTPException(404)
        j = conn.execute("SELECT text FROM application_jobs WHERE application_id=?", (app_id,)).fetchone()
        people = [dict(r) for r in conn.execute(
            "SELECT id, name, title, stage, outreach_sent_at FROM contacts WHERE application_id=? AND kind='Recruiter' ORDER BY id", (app_id,))]
    return {"application": dict(a), "job_text": j["text"] if j else "", "recruiters": people}


def _application(conn, app_id: int) -> dict:
    a = conn.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
    if not a:
        raise HTTPException(404, "Application not found.")
    return dict(a)


@router.post("/outreach/recruiter")
def make_recruiter_draft(body: RecruiterRequest):
    """A LinkedIn note and an email to a recruiter about one application. The recruiter becomes (or updates) a contact linked to it."""
    tone, length = _tone(body.tone), _length(body.length)
    url, slug = drafting.clean_profile_url(body.url)
    with db.session() as conn:
        app = _application(conn, body.application_id)
        saved = conn.execute("SELECT text FROM application_jobs WHERE application_id=?", (body.application_id,)).fetchone()
        job_text = body.job_text.strip() or (saved["text"] if saved else "")
        if body.job_text.strip():
            conn.execute("INSERT INTO application_jobs (application_id, text) VALUES (?,?) ON CONFLICT(application_id) DO UPDATE SET text=excluded.text",
                         (body.application_id, body.job_text.strip()[:6000]))
        c = conn.execute("SELECT * FROM contacts WHERE id=?", (body.contact_id,)).fetchone() if body.contact_id \
            else (outreach.find_contact(conn, body.name, slug) if (body.name.strip() or slug) else None)
        name = body.name.strip() or (c["name"] if c else drafting.name_from_slug(slug))
        profile = body.profile_text.strip() or (c["profile_text"] if c else "")
    try:
        d = recruiter.draft_recruiter(application=app, recruiter_name=name, profile_text=profile, tone=tone, length=length,
                                      instructions=body.instructions, job_text=job_text)
    except llm.LLMExhausted as e:
        raise HTTPException(429, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    with db.session() as conn:
        shown = name or f"Recruiter at {app['company']}"
        if c is None:
            cid = conn.execute(
                """INSERT INTO contacts (name, company, title, kind, stage, link, channel, source, ext_key, application_id, needs_triage)
                   VALUES (?,?,?,?,?,?,?,?,?,?,0)""",
                (shown, app["company"], "Recruiter", "Recruiter", "To reach out", url, "LinkedIn" if url else "Email", "manual", slug, body.application_id)).lastrowid
        else:
            cid = c["id"]
            conn.execute("""UPDATE contacts SET kind='Recruiter', application_id=?, needs_triage=0,
                              link = CASE WHEN link='' THEN ? ELSE link END, ext_key = CASE WHEN ext_key='' THEN ? ELSE ext_key END,
                              company = CASE WHEN company='' THEN ? ELSE company END WHERE id=?""",
                         (body.application_id, url, slug, app["company"], cid))
        conn.execute("UPDATE contacts SET profile_text=?, draft_json=? WHERE id=?", (body.profile_text.strip() or profile, json.dumps(d), cid))
        row = conn.execute("SELECT id, name, company, title, stage, link, application_id, outreach_sent_at FROM contacts WHERE id=?", (cid,)).fetchone()
    return {"contact": dict(row), "draft": d}


_SENT = re.compile(r"\[(\d{4}-\d{2}-\d{2})\] Sent (connection note|message|email|follow-up)[^\n]*\n?", re.I)


def last_sent(notes: str, first_only: bool = False) -> tuple[str, str]:
    """(date, text) of the most recent thing you logged as sent (the message to follow up on), from the contact's notes."""
    parts = list(_SENT.finditer(notes or ""))
    if not parts:
        return "", ""
    i = 0 if first_only else len(parts) - 1
    end = parts[i + 1].start() if i + 1 < len(parts) else len(notes)
    return parts[i].group(1), notes[parts[i].end():end].strip()


@router.post("/outreach/followup/{contact_id}")
def make_followup(contact_id: int, body: FollowupRequest):
    """A short nudge for someone you already messaged and who hasn't replied. Saved beside the original draft."""
    with db.session() as conn:
        c = conn.execute("SELECT * FROM contacts WHERE id=?", (contact_id,)).fetchone()
        if not c:
            raise HTTPException(404)
        app = _application(conn, c["application_id"]) if c["application_id"] else None
    when, previous = last_sent(c["notes"])
    if not when:
        raise HTTPException(422, "Log your first message with “I sent it” first, so there is something to follow up on.")
    days = (date.today() - date.fromisoformat(when)).days
    try:
        d = recruiter.draft_followup(contact=dict(c), previous_text=previous, days_since=days, application=app, profile_text=c["profile_text"],
                                     tone=_tone(body.tone), instructions=body.instructions)
    except llm.LLMExhausted as e:
        raise HTTPException(429, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    with db.session() as conn:
        saved = json.loads(c["draft_json"]) if c["draft_json"] else {}
        conn.execute("UPDATE contacts SET draft_json=? WHERE id=?", (json.dumps({**saved, "followup": d}), contact_id))
    return {"contact": {k: c[k] for k in ("id", "name", "company", "title", "stage", "link", "application_id", "outreach_sent_at")}, "followup": d}


@router.post("/contacts/{contact_id}/mark-sent")
def mark_sent(contact_id: int, body: SentRequest):
    """You sent it on LinkedIn: log the outreach (stage, date, channel) and keep the text you sent."""
    labels = {"note": "connection note", "message": "message", "email": "email", "followup": "follow-up"}
    if body.which not in labels:
        raise HTTPException(422, f"which must be one of {list(labels)}")
    today = date.today().isoformat()
    label, where = labels[body.which], ("by email" if body.which == "email" else "on LinkedIn")
    with db.session() as conn:
        c = conn.execute("SELECT notes, stage FROM contacts WHERE id=?", (contact_id,)).fetchone()
        if not c:
            raise HTTPException(404)
        entry = f"[{today}] Sent {label} {where}:\n{body.text.strip()}" if body.text.strip() else f"[{today}] Sent {label} {where}"
        conn.execute(
            """UPDATE contacts SET stage = CASE WHEN stage='To reach out' THEN 'Reached out' ELSE stage END,
                 outreach_sent_at = COALESCE(outreach_sent_at, ?), last_contacted_at=?, channel = CASE WHEN channel='' THEN ? ELSE channel END,
                 needs_triage=0, notes = ? WHERE id=?""",
            (today, today, "Email" if body.which == "email" else "LinkedIn", (c["notes"] + "\n\n" if c["notes"] else "") + entry, contact_id))
    return {"ok": True, "stage": "Reached out" if c["stage"] == "To reach out" else c["stage"]}
