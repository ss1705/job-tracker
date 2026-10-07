import csv
import io
import threading
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, db, followups, gmail, parser, scheduler, sync

@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    with db.session() as conn:   # one-time: applied dates were previously cut from UTC timestamps
        if not conn.execute("SELECT 1 FROM meta WHERE key='applied_at_local_v1'").fetchone():
            for (app_id,) in conn.execute("SELECT DISTINCT application_id FROM events").fetchall():
                sync.recompute(conn, app_id)
            conn.execute("INSERT INTO meta (key, value) VALUES ('applied_at_local_v1', '1')")
    scheduler.start()
    yield


app = FastAPI(title="Job Tracker", lifespan=lifespan)
STATIC = Path(__file__).resolve().parent.parent / "static"


class NewApplication(BaseModel):
    company: str = Field(min_length=1)
    role: str = ""
    status: str = "Applied"
    job_type: str = ""
    location: str = ""
    link: str = ""
    notes: str = ""
    applied_at: str | None = None
    deadline: str | None = None
    follow_up_at: str | None = None


class Patch(BaseModel):
    company: str | None = None
    role: str | None = None
    status: str | None = None
    job_type: str | None = None
    location: str | None = None
    link: str | None = None
    notes: str | None = None
    applied_at: str | None = None
    deadline: str | None = None
    follow_up_at: str | None = None
    needs_review: bool | None = None


class SyncRequest(BaseModel):
    since: date | None = None
    until: date | None = None


def _check_status(s: str):
    if s not in config.STATUSES:
        raise HTTPException(422, f"status must be one of {config.STATUSES}")


@app.get("/api/applications")
def list_applications():
    with db.session() as conn:
        rows = conn.execute(
            """SELECT a.*, (SELECT COUNT(*) FROM events e WHERE e.application_id = a.id) AS event_count
               FROM applications a
               ORDER BY COALESCE(a.last_event_at, a.applied_at, a.created_at) DESC"""
        ).fetchall()
    apps = [dict(r) for r in rows]
    for a in apps:
        a["applied_dates"] = []
    with db.session() as conn:
        by_id = {a["id"]: a for a in apps}
        for r in conn.execute("SELECT application_id, received_at, subject FROM events WHERE type='Applied' ORDER BY received_at"):
            if r["application_id"] in by_id and parser.is_receipt_subject(r["subject"]):
                d = sync.local_date(r["received_at"])      # a repeat = a receipt on a different day
                dates = by_id[r["application_id"]]["applied_dates"]
                if d not in dates:
                    dates.append(d)
        responded = {r[0] for r in conn.execute(
            "SELECT DISTINCT application_id FROM events WHERE type IN ('OA','Interview','Offer','Rejected')")}
    today = date.today()
    for a in apps:
        last_local = sync.local_date(a["last_event_at"]) if a["last_event_at"] else None
        latest = {**a, "applied_at": a["applied_dates"][-1] if a["applied_dates"] else a["applied_at"]}   # re-applying restarts the clock
        a.update(followups.flags(latest, a["id"] in responded, last_local, today))
    return apps


@app.post("/api/applications", status_code=201)
def create_application(body: NewApplication):
    _check_status(body.status)
    with db.session() as conn:
        cur = conn.execute(
            """INSERT INTO applications
               (company, company_key, role, job_type, location, link, notes, source, status, status_locked, applied_at,
                deadline, follow_up_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (body.company.strip(), db.company_key(body.company), body.role, body.job_type, body.location,
             body.link, body.notes, "Manual", body.status,
             0 if body.status == "Saved" else 1,    # a saved posting should advance when its confirmation email arrives
             body.applied_at, body.deadline or None, body.follow_up_at or None),
        )
        return {"id": cur.lastrowid}


@app.patch("/api/applications/{app_id}")
def update_application(app_id: int, body: Patch):
    fields = body.model_dump(exclude_unset=True)
    for k in ("deadline", "follow_up_at", "applied_at"):
        if k in fields and not fields[k]:
            fields[k] = None
    if "status" in fields:
        _check_status(fields["status"])
        with db.session() as conn:
            n_events = conn.execute("SELECT COUNT(*) FROM events WHERE application_id=?", (app_id,)).fetchone()[0]
        # user override stops email from changing the status, except moving an email-less row Saved -> Applied
        fields["status_locked"] = 0 if (fields["status"] in ("Saved", "Applied") and n_events == 0) else 1
        if fields["status"] == "Applied" and n_events == 0:
            fields.setdefault("applied_at", date.today().isoformat())
    if "needs_review" in fields:
        fields["needs_review"] = int(fields["needs_review"])
    if "company" in fields:
        fields["company_key"] = db.company_key(fields["company"])
    if not fields:
        return {"ok": True}
    sets = ", ".join(f"{k}=?" for k in fields)
    with db.session() as conn:
        cur = conn.execute(f"UPDATE applications SET {sets} WHERE id=?", (*fields.values(), app_id))
        if not cur.rowcount:
            raise HTTPException(404)
    return {"ok": True}


@app.post("/api/applications/{app_id}/followed-up")
def followed_up(app_id: int):
    """User chased the company: restart the no-response clock and clear any reminder date."""
    with db.session() as conn:
        cur = conn.execute("UPDATE applications SET followed_up_at=?, follow_up_at=NULL WHERE id=?",
                           (date.today().isoformat(), app_id))
        if not cur.rowcount:
            raise HTTPException(404)
    return {"ok": True}


@app.post("/api/applications/{app_id}/snooze")
def snooze(app_id: int, days: int = 7):
    remind = (date.today() + timedelta(days=max(1, days))).isoformat()
    with db.session() as conn:
        cur = conn.execute("UPDATE applications SET follow_up_at=? WHERE id=?", (remind, app_id))
        if not cur.rowcount:
            raise HTTPException(404)
    return {"follow_up_at": remind}


@app.delete("/api/applications/{app_id}", status_code=204)
def delete_application(app_id: int):
    with db.session() as conn:
        conn.execute("DELETE FROM applications WHERE id=?", (app_id,))


@app.get("/api/applications/{app_id}/block-preview")
def block_preview(app_id: int):
    """Which sender domains 'delete & block' would block, and which senders are too shared to block."""
    with db.session() as conn:
        senders = {r[0] for r in conn.execute("SELECT sender FROM events WHERE application_id=?", (app_id,))}
    blockable = sorted({p for s in senders if (p := parser.blockable_pattern(s))})
    return {"will_block": blockable, "skipped_shared": len(senders) - len([s for s in senders if parser.blockable_pattern(s)])}


@app.post("/api/applications/{app_id}/delete-and-block")
def delete_and_block(app_id: int):
    with db.session() as conn:
        senders = {r[0] for r in conn.execute("SELECT sender FROM events WHERE application_id=?", (app_id,))}
        patterns = sorted({p for s in senders if (p := parser.blockable_pattern(s))})
        for pat in patterns:
            conn.execute("INSERT OR IGNORE INTO blocked_senders (pattern) VALUES (?)", (pat,))
        conn.execute("DELETE FROM applications WHERE id=?", (app_id,))
    return {"deleted": app_id, "blocked": patterns}


@app.get("/api/blocked")
def list_blocked():
    with db.session() as conn:
        return [r[0] for r in conn.execute("SELECT pattern FROM blocked_senders ORDER BY pattern")]


@app.delete("/api/blocked/{pattern}", status_code=204)
def unblock(pattern: str):
    with db.session() as conn:
        conn.execute("DELETE FROM blocked_senders WHERE pattern=?", (pattern,))


@app.get("/api/applications/{app_id}/events")
def application_events(app_id: int):
    with db.session() as conn:
        rows = conn.execute(
            "SELECT type, received_at, subject, sender, snippet FROM events WHERE application_id=? ORDER BY received_at",
            (app_id,),
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/stats")
def stats():
    with db.session() as conn:
        by_status = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) n FROM applications GROUP BY status")}
        review = conn.execute("SELECT COUNT(*) FROM applications WHERE needs_review=1").fetchone()[0]
    total = sum(by_status.values())
    responded = sum(n for s, n in by_status.items() if s in ("OA", "Interview", "Offer", "Rejected"))
    apps = list_applications()
    return {"total": total, "by_status": by_status, "needs_review": review,
            "response_rate": round(responded / total, 3) if total else 0,
            "follow_up": sum(a["follow_up_due"] for a in apps),
            "deadline_soon": sum(1 for a in apps if a["deadline_days"] is not None and a["deadline_days"] <= 3)}


@app.post("/api/sync")
def start_sync(body: SyncRequest):
    if sync.state.running:
        return {"started": False, "reason": "already running"}
    try:
        gmail.authorize()  # fail fast with a clear message instead of in the background thread
    except gmail.AuthRequired as e:
        raise HTTPException(401, str(e))
    since = body.since or (date.today() - timedelta(days=90))
    sync.state.reset()
    sync.state.running = True  # visible immediately to the first poll
    threading.Thread(target=sync.run_sync, args=(since, body.until), daemon=True).start()
    return {"started": True, "since": since.isoformat()}


@app.get("/api/sync/status")
def sync_status():
    s = sync.state
    return {"running": s.running, "total": s.total, "done": s.done, "added": s.added, "updated": s.updated,
            "ignored": s.ignored, "needs_review": s.review, "failed": s.failed, "error": s.error, "message": s.message}


@app.get("/api/sync/last")
def last_sync():
    import json
    with db.session() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key='last_sync'").fetchone()
    return {**(json.loads(row[0]) if row else {}), "auto_minutes": config.AUTO_SYNC_MINUTES}


@app.get("/api/export.csv")
def export_csv():
    with db.session() as conn:
        rows = conn.execute(
            "SELECT company, role, job_type, location, status, applied_at, last_event_at, deadline, follow_up_at, source, link, notes FROM applications ORDER BY applied_at DESC"
        ).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["company", "role", "type", "location", "status", "applied", "last_update", "deadline", "reminder", "source", "link", "notes"])
    w.writerows([tuple(r) for r in rows])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=applications.csv"})


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
