"""Contacts: coffee chats, referrals, recruiters; with reminders and an optional link to an application."""
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import config, db, followups

router = APIRouter(prefix="/api/contacts", tags=["contacts"])

_EDITABLE = ("name", "company", "title", "kind", "stage", "link", "chat_date", "last_contacted_at",
             "remind_on", "notes", "application_id", "channel", "outreach_sent_at", "responded_at", "response_kind")
_DATES = ("chat_date", "last_contacted_at", "remind_on", "outreach_sent_at", "responded_at")


class NewContact(BaseModel):
    name: str = Field(min_length=1)
    company: str = ""
    title: str = ""
    kind: str = "Coffee chat"
    stage: str = "To reach out"
    link: str = ""
    chat_date: str | None = None
    remind_on: str | None = None
    notes: str = ""
    application_id: int | None = None
    channel: str = ""
    outreach_sent_at: str | None = None


class ContactPatch(BaseModel):
    name: str | None = None
    company: str | None = None
    title: str | None = None
    kind: str | None = None
    stage: str | None = None
    link: str | None = None
    chat_date: str | None = None
    last_contacted_at: str | None = None
    remind_on: str | None = None
    notes: str | None = None
    application_id: int | None = None
    channel: str | None = None
    outreach_sent_at: str | None = None
    responded_at: str | None = None
    response_kind: str | None = None


def _validate(fields: dict):
    if fields.get("kind") and fields["kind"] not in config.CONTACT_KINDS:
        raise HTTPException(422, f"kind must be one of {config.CONTACT_KINDS}")
    if fields.get("stage") and fields["stage"] not in config.CONTACT_STAGES:
        raise HTTPException(422, f"stage must be one of {config.CONTACT_STAGES}")


def _sync_application(conn, contact_id: int):
    """Linking a contact to an application switches that application's follow-up reminders on."""
    c = conn.execute("SELECT name, kind, application_id FROM contacts WHERE id=?", (contact_id,)).fetchone()
    if c and c["application_id"] and c["kind"] in config.CONTACT_TYPES:
        conn.execute("UPDATE applications SET contact_type=?, contact_name=? WHERE id=? AND contact_type=''",
                     (c["kind"], c["name"], c["application_id"]))


def list_with_flags(conn) -> list[dict]:
    rows = conn.execute(
        """SELECT c.*, a.company AS app_company, a.role AS app_role
           FROM contacts c LEFT JOIN applications a ON a.id = c.application_id
           ORDER BY COALESCE(c.last_contacted_at, c.chat_date, c.created_at) DESC""").fetchall()
    today, out = date.today(), []
    for r in rows:
        c = dict(r)
        c["has_draft"] = bool(c.pop("draft_json", ""))
        c.pop("profile_text", None)
        c["application_label"] = f"{c['app_company']} — {c['app_role'] or 'no role'}" if c["app_company"] else ""
        f = followups.contact_flags(c, today)
        c["follow_up_due"], c["follow_up_reason"] = f["due"], f["reason"]
        out.append(c)
    return out


@router.get("")
def list_contacts():
    with db.session() as conn:
        return list_with_flags(conn)


@router.post("", status_code=201)
def create_contact(body: NewContact):
    fields = body.model_dump()
    _validate(fields)
    for k in _DATES:
        fields[k] = fields.get(k) or None
    fields["name"], fields["company"] = fields["name"].strip(), fields["company"].strip()
    with db.session() as conn:
        cur = conn.execute(f"INSERT INTO contacts ({','.join(fields)}) VALUES ({','.join('?' * len(fields))})", tuple(fields.values()))
        _sync_application(conn, cur.lastrowid)
        return {"id": cur.lastrowid}


@router.patch("/{contact_id}")
def update_contact(contact_id: int, body: ContactPatch):
    fields = {k: v for k, v in body.model_dump(exclude_unset=True).items() if k in _EDITABLE}
    _validate(fields)
    for k in _DATES:
        if k in fields and not fields[k]:
            fields[k] = None
    if not fields:
        return {"ok": True}
    sets = ", ".join(f"{k}=?" for k in fields)
    with db.session() as conn:
        cur = conn.execute(f"UPDATE contacts SET {sets} WHERE id=?", (*fields.values(), contact_id))
        if not cur.rowcount:
            raise HTTPException(404)
        _sync_application(conn, contact_id)
    return {"ok": True}


@router.delete("/{contact_id}", status_code=204)
def delete_contact(contact_id: int):
    with db.session() as conn:
        conn.execute("DELETE FROM contacts WHERE id=?", (contact_id,))


class KeepRequest(BaseModel):
    kind: str | None = None


@router.post("/{contact_id}/keep")
def keep_contact(contact_id: int, body: KeepRequest):
    """Sort an auto-added person into your contacts (optionally choosing their kind): reminders switch on."""
    _validate({"kind": body.kind})
    with db.session() as conn:
        cur = conn.execute("UPDATE contacts SET needs_triage=0, kind=COALESCE(?, kind) WHERE id=?", (body.kind, contact_id))
        if not cur.rowcount:
            raise HTTPException(404)
        _sync_application(conn, contact_id)
    return {"ok": True}


@router.post("/{contact_id}/ignore")
def ignore_contact(contact_id: int):
    """Not someone to track: remove them and never auto-add them again."""
    from . import outreach
    with db.session() as conn:
        c = conn.execute("SELECT name, ext_key FROM contacts WHERE id=?", (contact_id,)).fetchone()
        if not c:
            raise HTTPException(404)
        conn.execute("INSERT INTO ignored_people (slug, name_key) VALUES (?,?)",
                     (c["ext_key"], "" if c["ext_key"] else outreach.norm_name(c["name"])))
        conn.execute("DELETE FROM contacts WHERE id=?", (contact_id,))
    return {"ok": True}


@router.post("/triage/keep-all")
def keep_all(body: KeepRequest):
    _validate({"kind": body.kind})
    with db.session() as conn:
        n = conn.execute("UPDATE contacts SET needs_triage=0, kind=COALESCE(?, kind) WHERE needs_triage=1", (body.kind,)).rowcount
    return {"kept": n}


@router.post("/{contact_id}/followed-up")
def followed_up(contact_id: int):
    """You've been in touch: restart the clock and clear the reminder date."""
    with db.session() as conn:
        cur = conn.execute("UPDATE contacts SET last_contacted_at=?, remind_on=NULL WHERE id=?",
                           (date.today().isoformat(), contact_id))
        if not cur.rowcount:
            raise HTTPException(404)
    return {"ok": True}


@router.post("/{contact_id}/snooze")
def snooze(contact_id: int, days: int = 7):
    remind = (date.today() + timedelta(days=max(1, days))).isoformat()
    with db.session() as conn:
        cur = conn.execute("UPDATE contacts SET remind_on=? WHERE id=?", (remind, contact_id))
        if not cur.rowcount:
            raise HTTPException(404)
    return {"remind_on": remind}
