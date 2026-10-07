"""Copy hand-edited statuses/notes/links from an old DB into a freshly synced one.

    .venv/bin/python scripts/carry_over_edits.py data/jobtracker.db
Matches on company (and role when a company has several applications). Never modifies the old DB.
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from jobtracker import db  # noqa: E402

old = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
old.row_factory = sqlite3.Row
edited = old.execute(
    "SELECT * FROM applications WHERE status_locked=1 OR notes<>'' OR link<>'' OR source='Manual'").fetchall()

with db.session() as conn:
    for o in edited:
        key = db.company_key(o["company"])
        cands = [r for r in conn.execute("SELECT * FROM applications").fetchall()
                 if r["company_key"] == key or (key and (key in r["company_key"] or r["company_key"] in key))]
        if o["source"] == "Manual" and not cands:
            conn.execute(
                """INSERT INTO applications (company, company_key, role, job_type, location, link, notes, source, status, status_locked, applied_at)
                   VALUES (?,?,?,?,?,?,?,?,?,1,?)""",
                (o["company"], key, o["role"], o["job_type"], o["location"], o["link"], o["notes"], "Manual", o["status"], o["applied_at"]))
            print(f"re-added manual entry: {o['company']}")
            continue
        if not cands:
            print(f"NO MATCH for {o['company']!r} / {o['role']!r} -- re-apply by hand")
            continue
        best = max(cands, key=lambda r: db.role_similarity(o["role"], r["role"]))
        conn.execute("UPDATE applications SET status=?, status_locked=1, notes=CASE WHEN ?<>'' THEN ? ELSE notes END, "
                     "link=CASE WHEN ?<>'' THEN ? ELSE link END, needs_review=0 WHERE id=?",
                     (o["status"], o["notes"], o["notes"], o["link"], o["link"], best["id"]))
        print(f"carried over: {o['company']} -> {best['company']} / {best['role']!r}: {o['status']}")
