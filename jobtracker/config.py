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
CONTACT_KINDS = ["Coffee chat", "Referral", "Recruiter", "Alum", "Other"]
CONTACT_STAGES = ["To reach out", "Reached out", "Chat scheduled", "Chatted", "Referred", "Closed"]
