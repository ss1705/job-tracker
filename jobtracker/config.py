import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DB_PATH = ROOT / os.getenv("DB_PATH", "data/jobtracker_v3.db")
GMAIL_CLIENT_FILE = ROOT / os.getenv("GMAIL_CLIENT_FILE", "secrets/gmail_client.json")
GMAIL_TOKEN_FILE = ROOT / os.getenv("GMAIL_TOKEN_FILE", "secrets/token.json")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")

STATUSES = ["Saved", "Applied", "OA", "Interview", "Offer", "Rejected", "Withdrawn"]
# what an email can be classified as: an application status, or "Action" (they need something from you: a form, documents, verification)
EMAIL_STATUSES = [*STATUSES, "Action"]
# tried in order when the primary model's daily token quota (free tier: 200k/day per model) is used up
GROQ_FALLBACK_MODELS = [m.strip() for m in os.getenv("GROQ_FALLBACK_MODELS", "openai/gpt-oss-20b,qwen/qwen3.8-27b").split(",") if m.strip()]
# extra sender substrings (domains or addresses) to ignore, e.g. BLOCKED_SENDERS=somecommunity.org,jobs@example.com
EXTRA_BLOCKED_SENDERS = tuple(x.strip().lower() for x in os.getenv("BLOCKED_SENDERS", "").split(",") if x.strip())
# timezone used for "applied on" dates (IANA name, e.g. America/Los_Angeles); defaults to this machine's timezone
TIMEZONE = os.getenv("TIMEZONE", "")
# background sync while the server is running (0 disables)
AUTO_SYNC_MINUTES = int(os.getenv("AUTO_SYNC_MINUTES", "120"))
AUTO_SYNC_LOOKBACK_DAYS = int(os.getenv("AUTO_SYNC_LOOKBACK_DAYS", "3"))
# follow-up reminders: only for applications with a contact (referral / coffee chat / recruiter); see followups.py
FOLLOWUP_DAYS = int(os.getenv("FOLLOWUP_DAYS", "7"))
FOLLOWUP_MAX_DAYS = int(os.getenv("FOLLOWUP_MAX_DAYS", "30"))
CHECKIN_DAYS = int(os.getenv("CHECKIN_DAYS", "7"))
CONTACT_TYPES = ["Referral", "Coffee chat", "Recruiter"]
CONTACT_KINDS = ["Cold outreach", "Coffee chat", "Referral", "Recruiter", "Alum", "Friend / colleague", "Other"]
CONTACT_STAGES = ["To reach out", "Reached out", "Replied", "Chat scheduled", "Chatted", "Referred", "Closed"]
# daily digest email (sent to yourself through Gmail; needs the send permission)
DIGEST_ENABLED = os.getenv("DIGEST_ENABLED", "1") not in ("0", "false", "False", "")
DIGEST_HOUR = int(os.getenv("DIGEST_HOUR", "8"))                      # local time; sent on the first check after this hour
DIGEST_SEND_EMPTY = os.getenv("DIGEST_SEND_EMPTY", "0") in ("1", "true", "True")

# outreach drafting: what you're asking for, in your own words (used in every draft)
OUTREACH_GOAL = os.getenv("OUTREACH_GOAL", "I'm looking for new grad / co-op software and ML roles and would value a short conversation or advice. "
                          "I am not asking for a referral up front.")
DATA_DIR = DB_PATH.parent
LINKEDIN_NOTE_MAX = int(os.getenv("LINKEDIN_NOTE_MAX", "200"))      # connection-note limit: 200 on free accounts, 300 on Premium

# developer tool: the Evaluate tab samples emails from your mailbox and stores up to 4,000 characters of each in data/ so you can
# hand-label them and measure accuracy. Off by default; nothing about it is reachable unless you turn it on.
ENABLE_EVAL = os.getenv("ENABLE_EVAL", "0") in ("1", "true", "True", "yes")

# optional password for the whole web app (strongly recommended before exposing it beyond this Mac, e.g. over Tailscale)
APP_PASSWORD = os.getenv("APP_PASSWORD", "")
