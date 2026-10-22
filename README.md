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

## Troubleshooting: "Gmail is disconnected" / "invalid_grant"

The page shows a red banner (and the sync/digest status lines say so) when Google has ended the saved Gmail connection. The usual
cause: your Google Cloud OAuth consent screen is in **Testing** mode, where Google expires the connection after about 7 days.

1. **Fix the cause first:** Google Cloud Console → your project → *Google Auth Platform* (or *APIs & Services → OAuth consent screen*) →
   *Audience* / *Publishing status* → **Publish app** (move from *Testing* to *In production*). For personal use it can stay unverified;
   Google shows an "unverified app" screen at sign-in (Advanced → Go to the app).
2. **Then reconnect:** `.venv/bin/python -m jobtracker.gmail` and approve in the browser (connections created *after* publishing don't expire weekly).
3. Click **Check again** on the banner. The next sync picks up what was missed.

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
- **Spreadsheets:** the importer matches columns by name (name, company, "Role/Title", LinkedIn URL, dates, status, notes,
  "Referral given?"), maps statuses (*Not contacted → To reach out*, *Messaged → Reached out*, *Replied → Replied*) and keeps any
  other columns (priority, hook, next step…) as labelled lines in the contact's notes. Sheet contacts skip the To sort step.
- **Your sent outreach:** LinkedIn doesn't email you when you send an invite or message, so use **Import…** on the
  Contacts tab: LinkedIn's data export (`Invitations.csv`, `messages.csv`, `Connections.csv`) or your old spreadsheet (CSV).
  Imports preview first and are idempotent. Only outreach you initiated is imported.
- Extend the backfill range with a wide **Sync**; add `"reprocess_linkedin": true` to `POST /api/sync` to re-read seen notices.

### Drafting outreach (Outreach tab)

Upload your resume once (PDF / DOCX / TXT; stored in `data/`, gitignored). Then paste someone's LinkedIn link and their
profile text (select-all on their profile and copy; even a headline works). It checks for a shared USC background in code,
drafts a ≤200-character connection note and a follow-up message tying your resume to their work, shows the evidence for each
personal detail, and runs a second fact-check pass that flags anything the sources don't support. It never reads LinkedIn and
never sends anything: you copy the text, send it on LinkedIn, and press **I sent it**, which logs the outreach (stage, date,
channel, the text you sent) in Contacts. Set `OUTREACH_GOAL` in `.env` to change what you ask people for.

## Deadlines and interviews

OA and interview emails are scanned for dates ("complete by Oct 12, 11:59 PM ET", "you have 7 days", "interview on Tuesday at 2 PM PT").
An LLM proposes them and code verifies each one (the quoted sentence must exist in the email, relative phrases are computed from
the email's own date, times are converted to your timezone, implausible dates are dropped). They show in a **Coming up** strip, as a
badge on the application, in the daily digest, and each has an **Add to calendar** download (with reminders a day and two hours
before). A reschedule email moves the existing one. You can also add your own (a recruiter call, say) and mark items done.

## Action needed

Some emails aren't a status at all: the company needs something from you (an additional-information form, documents, email
verification, a work-authorization form). They're classified as **Action needed**, which flags the application with what was asked
(badge, Coming up strip, daily digest) without changing its status. It clears when you mark it done or a later email shows progress,
and a stated deadline becomes an *Action due* milestone. It only counts when the email is about a job application or comes from a
recruiting system, so "verify your email" from a bank or a cloud console is ignored, and "if we need more information we'll contact you"
in an ordinary receipt doesn't trigger it. You can also flag any application yourself (**Needs my action**).

## Evaluation (developer tool, off by default)

Parsing accuracy depends on *your* mail, so the repo ships the tool I used to measure it. Set `ENABLE_EVAL=1` in `.env` and an
**Evaluate** tab appears. Without it the evaluation endpoints don't exist (they return 404) and nothing is stored.

What it does: it samples real emails from your mailbox (some the search finds, some it doesn't), shows the parser's prediction, and
lets you confirm or correct it. **Compute metrics** then reports detection precision/recall, status/company/role accuracy with 95%
confidence intervals, per-class results and a confusion matrix, rules-only vs rules + LLM, and an estimate of how many real application
emails the Gmail search misses. **Scan for missed emails** reads the subject and sender of every email the search did not match and
queues the plausible ones, so the search-recall figure rests on an exhaustive check of the suspects plus a random sample of the rest.
**Re-run on all** re-scores the sample after you change the parser (your labels are kept). **Write report files** saves
`docs/EVAL.md` (aggregate numbers only, safe to share) and `data/eval_errors.md` (private).

**Privacy:** sampling stores up to 4,000 characters of each sampled email in your local database (`data/`, gitignored) until you
click **Delete evaluation data**, which erases the emails, predictions, labels and the error file. Reports already written to `docs/`
contain only numbers.

### How accurate is it?

These are **the author's results on the author's mailbox**; yours will differ, which is the reason the tool is included. Full report:
[`docs/EVAL.md`](docs/EVAL.md); method: [`docs/EVAL_PROTOCOL.md`](docs/EVAL_PROTOCOL.md).

On a **held-out test** (emails the parser was never tuned on, labeled blind, parser frozen first), over 95 real application emails:
status **85%** (95% CI 77–91%), company **97%**, role **62%**; detecting whether an email is an application: precision 88%, recall 98%.
The LLM adds a lot over rules alone (status 55% → 85%, role 9% → 62%). The earlier in-sample figures (status 98%) were measured on
emails the parser was tuned on and are shown in the report for comparison only. Known weak spots: role extraction, and "Action needed"
being predicted for some ordinary receipts.

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
