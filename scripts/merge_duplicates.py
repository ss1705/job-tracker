"""Merge duplicate applications left by company-name variants ("Maven" / "Maven Securities").

    .venv/bin/python scripts/merge_duplicates.py            # dry run: shows what would merge
    .venv/bin/python scripts/merge_duplicates.py --apply    # does it (events are moved, never dropped)
Two applications merge when their company names match (see db.same_company) and their roles are
compatible (one is blank, or they are >=80% similar).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from jobtracker import db, sync  # noqa: E402

apply = "--apply" in sys.argv
with db.session() as conn:
    apps = [dict(r) for r in conn.execute("SELECT * FROM applications ORDER BY id")]
    ev_count = {r[0]: r[1] for r in conn.execute("SELECT application_id, COUNT(*) FROM events GROUP BY 1")}
    merged_into: dict[int, int] = {}

    def root(i):
        while i in merged_into:
            i = merged_into[i]
        return i

    by_id = {a["id"]: a for a in apps}
    for a in apps:
        for b in apps:
            if b["id"] <= a["id"] or root(a["id"]) == root(b["id"]):
                continue
            ra, rb = by_id[root(a["id"])], by_id[root(b["id"])]
            if not db.same_company(ra["company"], rb["company"]):
                continue
            if ra["role"] and rb["role"] and db.role_similarity(ra["role"], rb["role"]) < 0.8:
                continue
            # keep the row with a role (then the locked one, then the one with more emails)
            keep, drop = sorted((ra, rb), key=lambda r: (not r["role"], not r["status_locked"], -ev_count.get(r["id"], 0), r["id"]))
            print(f"merge  {drop['company']!r} / {drop['role'][:30]!r}  ->  {keep['company']!r} / {keep['role'][:30]!r}")
            merged_into[drop["id"]] = keep["id"]
            if apply:
                conn.execute("UPDATE events SET application_id=? WHERE application_id=?", (keep["id"], drop["id"]))
                conn.execute(
                    """UPDATE applications SET
                         role = CASE WHEN role='' THEN ? ELSE role END,
                         req_id = CASE WHEN req_id='' THEN ? ELSE req_id END,
                         job_type = CASE WHEN job_type='' THEN ? ELSE job_type END,
                         notes = CASE WHEN notes='' THEN ? ELSE notes END,
                         link = CASE WHEN link='' THEN ? ELSE link END,
                         status = CASE WHEN status_locked=0 AND ?=1 THEN ? ELSE status END,
                         status_locked = status_locked OR ?
                       WHERE id=?""",
                    (drop["role"], drop["req_id"], drop["job_type"], drop["notes"], drop["link"],
                     drop["status_locked"], drop["status"], drop["status_locked"], keep["id"]))
                longer = max((keep["company"], drop["company"]), key=lambda n: len(db.company_words(n)))
                conn.execute("UPDATE applications SET company=?, company_key=? WHERE id=?", (longer, db.company_key(longer), keep["id"]))
                keep["company"] = longer
                conn.execute("DELETE FROM applications WHERE id=?", (drop["id"],))
                keep.update(role=keep["role"] or drop["role"])
                ev_count[keep["id"]] = ev_count.get(keep["id"], 0) + ev_count.get(drop["id"], 0)
    if apply:
        for i in {root(i) for i in merged_into}:
            sync.recompute(conn, i)
print(f"\n{len(merged_into)} duplicate(s) {'merged' if apply else 'found (dry run; add --apply)'}")
