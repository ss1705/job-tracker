# Job Tracker

Tracks job applications (co-op / new grad / internships) automatically from the "application received" and
status-update emails in your Gmail. Local-first: FastAPI + SQLite + a single-page web UI.

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env            # optionally add GROQ_API_KEY (LLM fallback for hard emails)
```

Gmail access: create an OAuth client (type *Desktop app*) in Google Cloud Console, enable the Gmail API,
save the JSON as `secrets/gmail_client.json`, then sign in once:

```bash
.venv/bin/python -m jobtracker.gmail
```

## Run

```bash
.venv/bin/uvicorn jobtracker.api:app --reload
# open http://127.0.0.1:8000
```

Pick a date range in the UI and press **Sync**. Re-syncing is safe (messages are tracked by Gmail id).
Rows the parser wasn't sure about are marked **review**. Editing a status by hand locks it against later auto-updates.

## Follow-ups and saved postings

- **Saved postings:** add one with status *Saved* and a deadline. It shows "due in 3d" / "overdue" badges and a
  **Mark applied** button. When the confirmation email arrives it attaches to the same row.
- **Reminders (only where there's someone to follow up with):** set an application's **Contact** to Referral, Coffee
  chat or Recruiter (+ a name). Those get flagged under the **Follow up** chip when there's been no news for
  `FOLLOWUP_DAYS` (7) after applying (until `FOLLOWUP_MAX_DAYS`, 30), or `CHECKIN_DAYS` (7) after an OA/interview.
  Cold applications never get automatic reminders, but any application can have a manual "Remind me on" date.
  **Followed up** restarts the clock, **Snooze 1 week** delays it.

## Auto-sync

While the server runs it re-syncs Gmail every `AUTO_SYNC_MINUTES` (default 120; `0` turns it off), looking back
`AUTO_SYNC_LOOKBACK_DAYS` (default 3). After a failed run (e.g. Groq quota) it waits an hour. To keep the server
running in the background from login, so syncing needs no action from you:

```bash
scripts/install_launchd.sh              # start at login (macOS)
scripts/install_launchd.sh uninstall
```

## How parsing works

`jobtracker/parser.py`: blocklist for job boards -> ATS/sender + subject rules (Greenhouse, Workday, iCIMS,
Lever, Ashby...) -> status phrase rules -> LLM fallback only when company/role/status are still missing.
Status for an application is derived from its email timeline (`sync.derive_status`).

## Dev

```bash
.venv/bin/python -m pytest -q
.venv/bin/python evaluate.py --rules -v     # score the parser on tests/labeled_emails.csv
```

`legacy/` holds the original Sheets-based prototype (git-ignored).
