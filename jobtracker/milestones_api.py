"""Endpoints for milestones (deadlines and interview times) and the one-off rescan of recent emails."""
import threading
from datetime import date

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from . import db, gmail, milestones, sync

router = APIRouter(prefix="/api", tags=["milestones"])
scan = {"running": False, "done": 0, "total": 0, "found": 0, "error": ""}
_lock = threading.Lock()


class NewMilestone(BaseModel):
    kind: str = "Interview"
    due_at: str                      # YYYY-MM-DD or YYYY-MM-DDTHH:MM, your local time
    note: str = ""


class MilestonePatch(BaseModel):
    kind: str | None = None
    due_at: str | None = None
    note: str | None = None
    done: bool | None = None


def _check(kind: str | None, due_at: str | None):
    if kind is not None and kind not in milestones.KINDS:
        raise HTTPException(422, f"kind must be one of {milestones.KINDS}")
    if due_at is not None:
        try:
            date.fromisoformat(due_at[:10])
            if len(due_at) > 10 and (len(due_at) != 16 or due_at[10] != "T"):
                raise ValueError
        except ValueError:
            raise HTTPException(422, "due_at must look like 2026-10-12 or 2026-10-12T23:59")


@router.get("/milestones")
def list_milestones(include_done: bool = False, horizon_days: int = 60):
    with db.session() as conn:
        return milestones.upcoming(conn, date.today(), horizon_days, include_done)


@router.post("/applications/{app_id}/milestones", status_code=201)
def add_milestone(app_id: int, body: NewMilestone):
    _check(body.kind, body.due_at)
    with db.session() as conn:
        if not conn.execute("SELECT 1 FROM applications WHERE id=?", (app_id,)).fetchone():
            raise HTTPException(404)
        cur = conn.execute("INSERT INTO milestones (application_id, kind, due_at, source, note) VALUES (?,?,?,?,?)",
                           (app_id, body.kind, body.due_at, "manual", body.note))
    return {"id": cur.lastrowid}


@router.patch("/milestones/{mid}")
def patch_milestone(mid: int, body: MilestonePatch):
    _check(body.kind, body.due_at)
    fields = {k: (int(v) if k == "done" else v) for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    if not fields:
        return {"ok": True}
    with db.session() as conn:
        cur = conn.execute(f"UPDATE milestones SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?", (*fields.values(), mid))
        if not cur.rowcount:
            raise HTTPException(404)
    return {"ok": True}


@router.delete("/milestones/{mid}", status_code=204)
def delete_milestone(mid: int):
    with db.session() as conn:
        conn.execute("DELETE FROM milestones WHERE id=?", (mid,))


@router.get("/milestones/{mid}.ics")
def milestone_ics(mid: int):
    with db.session() as conn:
        r = conn.execute("SELECT m.*, a.company, a.role FROM milestones m JOIN applications a ON a.id=m.application_id WHERE m.id=?", (mid,)).fetchone()
    if not r:
        raise HTTPException(404)
    return Response(milestones.ics(dict(r)), media_type="text/calendar",
                    headers={"Content-Disposition": f'attachment; filename="{r["kind"].replace(" ", "-").lower()}-{r["company"].replace(" ", "-").lower()}.ics"'})


class RescanRequest(BaseModel):
    days: int = 21


@router.post("/milestones/rescan")
def rescan(body: RescanRequest):
    """Look for deadlines in OA / interview emails already in the tracker (recent ones), once."""
    try:
        gmail.authorize()
    except gmail.AuthRequired as e:
        raise HTTPException(401, str(e))
    if not _lock.acquire(blocking=False):
        return {"started": False, "reason": "already running"}
    scan.update(running=True, done=0, total=0, found=0, error="")
    threading.Thread(target=_rescan, args=(max(1, min(body.days, 120)),), daemon=True).start()
    return {"started": True}


def _rescan(days: int):
    try:
        with db.session() as conn:
            rows = conn.execute(
                """SELECT e.gmail_id, e.application_id FROM events e
                   WHERE e.type IN ('OA','Interview') AND e.gmail_id IS NOT NULL AND e.received_at >= datetime('now', ?) ORDER BY e.received_at""",
                (f"-{days} day",)).fetchall()
        scan["total"] = len(rows)
        svc = gmail.service()
        for r in rows:
            msg = gmail.fetch_message(svc, r["gmail_id"])
            scan["found"] += sync.capture_milestones(r["application_id"], msg)
            scan["done"] += 1
    except Exception as e:
        scan["error"] = f"{type(e).__name__}: {e}"
    finally:
        scan["running"] = False
        _lock.release()


@router.get("/milestones/rescan/status")
def rescan_status():
    return scan
