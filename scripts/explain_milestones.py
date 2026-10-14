"""Explain why a deadline was or wasn't found in a company's recent OA / interview / action emails.

    .venv/bin/python scripts/explain_milestones.py maven
    .venv/bin/python scripts/explain_milestones.py maven --show-text

Run it yourself: it reads those emails from Gmail on your machine. By default it prints only what each step DECIDED (lengths,
yes/no, dates it computed), never the email text. --show-text adds the sentences that mention time limits, on your screen only.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from jobtracker import db, gmail, milestones as m  # noqa: E402

TIME_WORDS = re.compile(r"\b(days?|hours?|weeks?|deadline|expire|valid|within|due|before|until|by the end)\b", re.I)


def main(argv: list[str] | None = None, fetch=None, out=print) -> int:
    argv = sys.argv[1:] if argv is None else argv
    show_text = "--show-text" in argv
    names = [a for a in argv if not a.startswith("--")]
    company = names[0].lower() if names else ""
    with db.session() as conn:
        rows = conn.execute(
            """SELECT e.gmail_id, e.type, e.received_at, e.subject, a.company,
                      (SELECT outcome FROM milestone_scans s WHERE s.gmail_id = e.gmail_id) AS last_outcome,
                      (SELECT detail FROM milestone_scans s WHERE s.gmail_id = e.gmail_id) AS last_detail
               FROM events e JOIN applications a ON a.id = e.application_id
               WHERE e.type IN ('OA','Interview','Action') AND e.gmail_id IS NOT NULL AND lower(a.company) LIKE ?
               ORDER BY e.received_at DESC LIMIT 5""", (f"%{company}%",)).fetchall()
    if not rows:
        out(f"No OA / interview / action emails found for a company matching {company!r}.")
        return 1
    if fetch is None:
        svc = gmail.service()
        fetch = lambda gid: gmail.fetch_message(svc, gid)
    for r in rows:
        msg = fetch(r["gmail_id"])
        plain, html_text = msg["body"], msg.get("html_text", "")
        combined = f"{msg['subject']}\n{plain}\n{html_text}"
        by_rule = m.rule_deadlines(combined, msg["received_at"])
        res = m.extract_detailed(msg["subject"], plain, msg["received_at"], html_text)
        out(f"\n== {r['company']} | logged as {r['type']} | received {msg['received_at'][:16]} UTC | {msg['subject'][:70]}")
        out(f"   text: plain part {len(plain)} chars, HTML part {len(html_text)} chars "
            f"(the model reads the {'HTML' if len(html_text) > 1.3 * len(plain) else 'plain'} part)")
        out(f"   time-limit wording present: {'yes' if m._HAS_DATE_HINT.search(combined) else 'NO'}")
        out(f"   plain rule found: {[(x['kind'], x['due_at']) for x in by_rule] or 'nothing'}")
        out(f"   full result now: {res['outcome']} ({res['detail']}) -> {[(x['kind'], x['due_at']) for x in res['items']] or 'no dates'}")
        out(f"   last recorded scan: {r['last_outcome'] or 'never scanned'}" + (f" ({r['last_detail']})" if r["last_detail"] else ""))
        if show_text:
            sents = [" ".join(s.split()) for s in re.split(r"(?<=[.!?])\s+|\n+", f"{plain}\n{html_text}") if TIME_WORDS.search(s) and 10 < len(s.strip()) < 260]
            seen, shown = set(), 0
            for s in sents:
                if s not in seen and shown < 8:
                    seen.add(s)
                    shown += 1
                    out(f"   sentence: {s}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
