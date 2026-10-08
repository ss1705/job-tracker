import re
import sqlite3
from contextlib import contextmanager
from difflib import SequenceMatcher

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company TEXT NOT NULL,
    company_key TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT '',
    req_id TEXT NOT NULL DEFAULT '',
    location TEXT NOT NULL DEFAULT '',
    job_type TEXT NOT NULL DEFAULT '',      -- Co-op / New Grad / Internship / Full-time
    source TEXT NOT NULL DEFAULT '',        -- Email, Manual, ...
    link TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'Applied',
    status_locked INTEGER NOT NULL DEFAULT 0,  -- 1 once the user edits status by hand
    needs_review INTEGER NOT NULL DEFAULT 0,
    applied_at TEXT,                        -- ISO date
    last_event_at TEXT,
    deadline TEXT,                          -- for Saved postings: apply-by date
    follow_up_at TEXT,                      -- manual reminder date (a future date also snoozes auto reminders)
    followed_up_at TEXT,                    -- last time the user marked 'followed up'
    contact_type TEXT NOT NULL DEFAULT '',  -- Referral / Coffee chat / Recruiter: enables follow-up reminders
    contact_name TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_app_company ON applications(company_key);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    gmail_id TEXT UNIQUE,
    type TEXT NOT NULL,
    received_at TEXT NOT NULL,              -- ISO datetime
    subject TEXT NOT NULL DEFAULT '',
    sender TEXT NOT NULL DEFAULT '',
    snippet TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_event_app ON events(application_id);

-- senders the user chose to ignore ("delete & block"); matched as substrings of the sender address
CREATE TABLE IF NOT EXISTS blocked_senders (
    pattern TEXT PRIMARY KEY,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- people to keep in touch with: coffee chats, referrals, recruiters
CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    company TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'Coffee chat',
    stage TEXT NOT NULL DEFAULT 'To reach out',
    link TEXT NOT NULL DEFAULT '',            -- LinkedIn / profile URL
    chat_date TEXT,                           -- when the chat is scheduled / happened
    last_contacted_at TEXT,
    remind_on TEXT,                           -- manual reminder (a future date also snoozes the automatic ones)
    notes TEXT NOT NULL DEFAULT '',
    application_id INTEGER REFERENCES applications(id) ON DELETE SET NULL,
    channel TEXT NOT NULL DEFAULT '',         -- LinkedIn / Email / ...
    source TEXT NOT NULL DEFAULT 'manual',    -- manual / linkedin-import / linkedin-email / sheet-import
    ext_key TEXT NOT NULL DEFAULT '',         -- LinkedIn profile slug, for matching
    outreach_sent_at TEXT,                    -- when you first reached out (invite / message)
    responded_at TEXT,                        -- when they last responded (accepted / replied)
    response_kind TEXT NOT NULL DEFAULT '',   -- 'Accepted invite' / 'Replied'
    response_logged_at TEXT,                  -- when the tracker noticed (drives the digest)
    needs_triage INTEGER NOT NULL DEFAULT 0,  -- auto-added from LinkedIn: the user hasn't sorted it yet (no reminders until kept)
    profile_text TEXT NOT NULL DEFAULT '',    -- what you pasted from their profile (kept locally, for regenerating drafts)
    draft_json TEXT NOT NULL DEFAULT '',      -- last outreach draft (note, message, evidence, USC check)
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- LinkedIn people the user chose to ignore; never auto-added again
CREATE TABLE IF NOT EXISTS ignored_people (
    slug TEXT NOT NULL DEFAULT '',
    name_key TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- evaluation set: real emails sampled from your mailbox, with the pipeline's prediction and YOUR label (local only)
CREATE TABLE IF NOT EXISTS eval_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gmail_id TEXT UNIQUE NOT NULL,
    kind TEXT NOT NULL,                       -- matched (the sync query finds it) | unmatched (it doesn't)
    subject TEXT NOT NULL DEFAULT '',
    sender TEXT NOT NULL DEFAULT '',
    received_at TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    pred_json TEXT NOT NULL DEFAULT '',       -- {is_job, company, role, status, via} from the full pipeline at sampling time
    truth_json TEXT NOT NULL DEFAULT '',      -- {is_job, company, role, status} confirmed or corrected by you
    labeled_at TEXT
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

-- every Gmail message we've looked at, so re-syncs are cheap and idempotent
CREATE TABLE IF NOT EXISTS seen_messages (
    gmail_id TEXT PRIMARY KEY,
    outcome TEXT NOT NULL,                  -- ignored | linked | needs_review
    seen_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def session():
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with session() as conn:
        conn.executescript(SCHEMA)
        have = {r["name"] for r in conn.execute("PRAGMA table_info(applications)")}
        added = {"deadline": "TEXT", "follow_up_at": "TEXT", "followed_up_at": "TEXT",       # upgrade older databases
                 "contact_type": "TEXT NOT NULL DEFAULT ''", "contact_name": "TEXT NOT NULL DEFAULT ''"}
        for col, ddl in added.items():
            if col not in have:
                conn.execute(f"ALTER TABLE applications ADD COLUMN {col} {ddl}")
        have_c = {r["name"] for r in conn.execute("PRAGMA table_info(contacts)")}
        added_c = {"channel": "TEXT NOT NULL DEFAULT ''", "source": "TEXT NOT NULL DEFAULT 'manual'", "ext_key": "TEXT NOT NULL DEFAULT ''",
                   "outreach_sent_at": "TEXT", "responded_at": "TEXT", "response_kind": "TEXT NOT NULL DEFAULT ''",
                   "response_logged_at": "TEXT", "needs_triage": "INTEGER NOT NULL DEFAULT 0",
                   "profile_text": "TEXT NOT NULL DEFAULT ''", "draft_json": "TEXT NOT NULL DEFAULT ''"}
        for col, ddl in added_c.items():
            if col not in have_c:
                conn.execute(f"ALTER TABLE contacts ADD COLUMN {col} {ddl}")
        # one-time: people auto-added from LinkedIn before sorting existed go to "To sort" (only those never touched)
        if not conn.execute("SELECT 1 FROM meta WHERE key='triage_v1'").fetchone():
            conn.execute("""UPDATE contacts SET needs_triage=1
                            WHERE source IN ('linkedin-email','linkedin-import') AND stage IN ('Replied','Reached out')
                              AND notes='' AND application_id IS NULL AND remind_on IS NULL AND chat_date IS NULL
                              AND last_contacted_at IS NULL AND kind='Cold outreach'""")
            conn.execute("INSERT INTO meta (key, value) VALUES ('triage_v1', '1')")


_SUFFIXES = r"\b(inc|llc|ltd|corp|corporation|co|company|group|technologies|technology|holdings|services|people|labs|careers|the)\b"


def company_key(name: str) -> str:
    s = re.sub(r"[^a-z0-9 ]", " ", name.lower())
    s = re.sub(_SUFFIXES, " ", s)
    return re.sub(r"\s+", "", s)   # squash spaces: "Maven Securities" == "Mavensecurities" (domain-derived)


def _norm_role(role: str) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", role.lower()).strip()


def role_similarity(a: str, b: str) -> float:
    a, b = _norm_role(a), _norm_role(b)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def company_words(name: str) -> list[str]:
    s = re.sub(r"[^a-z0-9 ]", " ", name.lower())
    s = re.sub(_SUFFIXES, " ", s)
    return s.split()


def same_company(a: str, b: str) -> bool:
    """Same after normalising, or one name is a whole-word prefix of the other
    ("Maven" / "Maven Securities", "Hyperion" / "Hyperion Global AI Consulting"; not "Apple" / "Applebee's")."""
    if company_key(a) == company_key(b):
        return True
    wa, wb = company_words(a), company_words(b)
    short, long_ = (wa, wb) if len(wa) <= len(wb) else (wb, wa)
    return bool(short) and long_[:len(short)] == short and len("".join(short)) >= 4


def find_application(conn, company: str, role: str, req_id: str, is_applied_event: bool):
    """Return (row | None, confident: bool)."""
    rows = [r for r in conn.execute("SELECT * FROM applications").fetchall() if same_company(company, r["company"])]
    if not rows:
        return None, True
    if req_id:
        for r in rows:
            if r["req_id"] and r["req_id"] == req_id:
                return r, True
    if not role:
        # no role to go on (reminders, "verify your email", generic receipts): attach to the latest
        # application at that company instead of spawning a duplicate
        latest = max(rows, key=lambda r: r["last_event_at"] or r["created_at"] or "")
        return latest, True
    scored = sorted(((role_similarity(role, r["role"]), r) for r in rows), key=lambda t: -t[0])
    best_score, best = scored[0]
    if best_score >= 0.8:
        return best, True
    # an earlier role-less row for this company is most likely the same application: fill its role in
    blank = [r for r in rows if not r["role"]]
    if blank:
        return blank[0], True
    if is_applied_event:
        return None, True       # a fresh "thanks for applying" with a different role is a new application
    if len(rows) == 1:
        return rows[0], best_score > 0.5
    return scored[0][1], False
