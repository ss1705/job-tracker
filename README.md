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

## Contacts

The **Contacts** tab tracks people: coffee chats, referrals, recruiters, alumni. Each has a kind, a stage (*To reach
out → Reached out → Chat scheduled → Chatted → Referred → Closed*), a chat date, notes and an optional related
application (linking one turns on that application's follow-up reminders). Reminders: *Reached out* with no reply after
7 days, a thank-you nudge the day after a chat, a heads-up the day before a scheduled chat, plus any "Remind me on" date.

### LinkedIn outreach (only people *you* reach out to)

- **Responses, automatically:** each sync reads LinkedIn's notification emails. "*X accepted your invitation*" and
  "*X just messaged you*" update the matching contact (stage → *Replied*, with the date and kind). Incoming connection
  requests and messages from people you never contacted are ignored. People are matched by profile link, never by a bare
  first name.
- **You decide who counts:** not everyone you invite is outreach (friends, colleagues, classmates). People auto-added from
  LinkedIn (and from the LinkedIn export import) land in **To sort**: choose a kind (Cold outreach, Coffee chat, Referral,
  Recruiter, Alum, Friend / colleague, Other) to keep them, or **Ignore** to remove them for good. Unsorted people get no
  reminders and appear in the digest only as an "N to sort" line. Kept people still update automatically.
- **Your sent outreach:** LinkedIn doesn't email you when you send an invite or message, so use **Import…** on the
  Contacts tab: LinkedIn's data export (`Invitations.csv`, `messages.csv`, `Connections.csv`) or your old spreadsheet (CSV).
  Imports preview first and are idempotent. Only outreach you initiated is imported.
- Extend the backfill range with a wide **Sync**; add `"reprocess_linkedin": true` to `POST /api/sync` to re-read seen notices.

## Daily digest

A short email to yourself each morning (at `DIGEST_HOUR`, default 8) listing new responses, people and applications to
follow up with, and saved postings closing soon. Nothing to report = no email. It goes through Gmail so it reaches your
Mac and iPhone; it needs one extra permission, granted once:

```bash
.venv/bin/python -m jobtracker.gmail      # consent screen: allow "send email on your behalf" (only ever sent to yourself)
```

The page's **Daily digest** card has a preview and a "Send test email" button.

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
