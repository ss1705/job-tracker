"""HTTP endpoints for the evaluation set (sampling, labeling, metrics)."""
import json
from datetime import date, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import config, db, evalset, gmail

def _enabled():
    """The evaluation tools store email text, so they don't exist unless ENABLE_EVAL=1."""
    if not config.ENABLE_EVAL:
        raise HTTPException(404, "Evaluation tools are off. Set ENABLE_EVAL=1 in .env to turn them on.")


router = APIRouter(prefix="/api/eval", tags=["eval"], dependencies=[Depends(_enabled)])


class SampleRequest(BaseModel):
    since: date | None = None
    n_matched: int = 120
    n_unmatched: int = 60


class LabelRequest(BaseModel):
    is_job: bool
    company: str = ""
    role: str = ""
    status: str | None = None


@router.delete("/data")
def delete_data():
    """Erase everything the evaluation tools stored: sampled emails, predictions, your labels, the counts and the error file."""
    with db.session() as conn:
        n = conn.execute("SELECT COUNT(*) FROM eval_items").fetchone()[0]
        conn.execute("DELETE FROM eval_items")
        conn.execute("DELETE FROM meta WHERE key='eval_universe'")
    err = config.DATA_DIR / "eval_errors.md"
    had_errors = err.exists()
    if had_errors:
        err.unlink()
    return {"deleted_emails": n, "deleted_error_file": had_errors}


@router.get("/summary")
def summary():
    with db.session() as conn:
        return evalset.summary(conn)


@router.post("/sample")
def sample(body: SampleRequest):
    try:
        gmail.authorize()
    except gmail.AuthRequired as e:
        raise HTTPException(401, str(e))
    since = body.since or (date.today() - timedelta(days=120))
    if not evalset.start_sample(since, max(0, min(body.n_matched, 400)), max(0, min(body.n_unmatched, 400))):
        return {"started": False, "reason": "already running"}
    return {"started": True}


class PredictRequest(BaseModel):
    redo: bool = False        # re-run the pipeline on every sampled email (after improving the parser), replacing old predictions


@router.post("/scan")
def scan():
    """Read subject + sender of every email the search did not match; queue the plausible ones for labeling."""
    try:
        gmail.authorize()
    except gmail.AuthRequired as e:
        raise HTTPException(401, str(e))
    with db.session() as conn:             # same window as the original sample, so matched and unmatched counts cover the same dates
        u = conn.execute("SELECT value FROM meta WHERE key='eval_universe'").fetchone()
    since = date.fromisoformat(json.loads(u[0])["since"]) if u and json.loads(u[0]).get("since") else date.today() - timedelta(days=120)
    return {"started": evalset.start_scan(since)}


@router.post("/predict")
def predict(body: PredictRequest | None = None):
    return {"started": evalset.start_predict(redo=bool(body and body.redo))}


@router.get("/next")
def next_item(after_id: int = 0, kind: str | None = None):
    with db.session() as conn:
        return {"item": evalset.next_item(conn, after_id, kind if kind in ("matched", "unmatched") else None)}


@router.get("/labeled")
def labeled():
    with db.session() as conn:
        return evalset.list_labeled(conn)


@router.get("/item/{item_id}")
def item(item_id: int):
    with db.session() as conn:
        it = evalset.get_item(conn, item_id)
    if not it:
        raise HTTPException(404)
    return {"item": it}


@router.post("/{item_id}/unlabel")
def unlabel(item_id: int):
    with db.session() as conn:
        if not conn.execute("SELECT 1 FROM eval_items WHERE id=?", (item_id,)).fetchone():
            raise HTTPException(404)
        evalset.unlabel(conn, item_id)
        return evalset.summary(conn)


@router.post("/{item_id}/label")
def label(item_id: int, body: LabelRequest):
    if body.status is not None and body.status not in ("Applied", "OA", "Interview", "Offer", "Rejected", "Action", "Other"):
        raise HTTPException(422, "status must be Applied, OA, Interview, Offer, Rejected, Action or Other")
    with db.session() as conn:
        if not conn.execute("SELECT 1 FROM eval_items WHERE id=?", (item_id,)).fetchone():
            raise HTTPException(404)
        evalset.label(conn, item_id, body.model_dump())
        return evalset.summary(conn)


@router.get("/report")
def report():
    with db.session() as conn:
        s = evalset.summary(conn)
        if s["matched_labeled"] < 5:
            raise HTTPException(409, f"Label at least 5 emails first ({s['matched_labeled']} so far).")
        return evalset.report(conn)


@router.post("/write-report")
def write_report():
    """docs/EVAL.md (aggregate numbers only; safe to commit) and data/eval_errors.md (email text; gitignored)."""
    root = Path(__file__).resolve().parent.parent
    with db.session() as conn:
        rep = evalset.report(conn)
        errors = evalset.errors_markdown(conn)
    (root / "docs").mkdir(exist_ok=True)
    (root / "docs" / "EVAL.md").write_text(evalset.markdown(rep), encoding="utf-8")
    err_path = config.DATA_DIR / "eval_errors.md"
    err_path.write_text(errors, encoding="utf-8")
    err_path.chmod(0o600)
    return {"report": "docs/EVAL.md", "errors": str(err_path.relative_to(root)) if err_path.is_relative_to(root) else str(err_path)}
