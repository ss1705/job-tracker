"""HTTP endpoints for the resume and outreach drafting."""
import base64
import json
from datetime import date

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import db, drafting, llm, outreach, resume

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
        d = drafting.draft(name=name, profile_text=profile, url=url, usc_override=body.usc_override)
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
        c = conn.execute("SELECT id, name, company, title, stage, link, profile_text, draft_json FROM contacts WHERE id=?", (contact_id,)).fetchone()
    if not c:
        raise HTTPException(404)
    return {"contact": {k: c[k] for k in ("id", "name", "company", "title", "stage", "link")}, "profile_text": c["profile_text"],
            "draft": json.loads(c["draft_json"]) if c["draft_json"] else None}


@router.post("/contacts/{contact_id}/mark-sent")
def mark_sent(contact_id: int, body: SentRequest):
    """You sent it on LinkedIn: log the outreach (stage, date, channel) and keep the text you sent."""
    if body.which not in ("note", "message"):
        raise HTTPException(422, "which must be note or message")
    today = date.today().isoformat()
    label = "connection note" if body.which == "note" else "message"
    with db.session() as conn:
        c = conn.execute("SELECT notes, stage FROM contacts WHERE id=?", (contact_id,)).fetchone()
        if not c:
            raise HTTPException(404)
        entry = f"[{today}] Sent {label} on LinkedIn:\n{body.text.strip()}" if body.text.strip() else f"[{today}] Sent {label} on LinkedIn"
        conn.execute(
            """UPDATE contacts SET stage = CASE WHEN stage='To reach out' THEN 'Reached out' ELSE stage END,
                 outreach_sent_at = COALESCE(outreach_sent_at, ?), last_contacted_at=?, channel = CASE WHEN channel='' THEN 'LinkedIn' ELSE channel END,
                 needs_triage=0, notes = ? WHERE id=?""",
            (today, today, (c["notes"] + "\n\n" if c["notes"] else "") + entry, contact_id))
    return {"ok": True, "stage": "Reached out" if c["stage"] == "To reach out" else c["stage"]}
