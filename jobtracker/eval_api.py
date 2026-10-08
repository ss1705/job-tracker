"""HTTP endpoints for the evaluation set (sampling, labeling, metrics)."""
from datetime import date, timedelta
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import config, db, evalset, gmail

router = APIRouter(prefix="/api/eval", tags=["eval"])


class SampleRequest(BaseModel):
    since: date | None = None
    n_matched: int = 120
    n_unmatched: int = 60


class LabelRequest(BaseModel):
    is_job: bool
    company: str = ""
    role: str = ""
    status: str | None = None


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
    if not evalset.start_sample(since, max(10, min(body.n_matched, 400)), max(0, min(body.n_unmatched, 200))):
        return {"started": False, "reason": "already running"}
    return {"started": True}


@router.post("/predict")
def predict():
    return {"started": evalset.start_predict()}


@router.get("/next")
def next_item(after_id: int = 0, kind: str | None = None):
    with db.session() as conn:
        return {"item": evalset.next_item(conn, after_id, kind if kind in ("matched", "unmatched") else None)}


@router.post("/{item_id}/label")
def label(item_id: int, body: LabelRequest):
    if body.status is not None and body.status not in ("Applied", "OA", "Interview", "Offer", "Rejected", "Other"):
        raise HTTPException(422, "status must be Applied, OA, Interview, Offer, Rejected or Other")
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
