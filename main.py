from fastapi import FastAPI
from pydantic import BaseModel, field_validator
import gspread
from google.oauth2.service_account import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from google.auth.transport.requests import Request
import pickle
import os
import base64
import json
from groq import Groq
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from apscheduler.schedulers.background import BackgroundScheduler
from contextlib import asynccontextmanager

def scheduled_process():
    try:
        process_emails()
        print("✅ Nightly email sync complete")
    except Exception as e:
        print(f"❌ Nightly sync failed: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler = BackgroundScheduler()

    scheduler.add_job(
    scheduled_process,
    trigger="cron",
    hour=2,
    minute=0,
    timezone="America/Los_Angeles"
)

    scheduler.start()
    print("⏰ Scheduler started")
    yield
    scheduler.shutdown()

load_dotenv()

groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))

app = FastAPI(lifespan=lifespan)
applications = []

BLOCKED_DOMAINS = {
    "lensa.com", "indeed.com", "linkedin.com", "glassdoor.com",
    "ziprecruiter.com", "handshake.com", "business.amazon.com",
    "amazon.com", "datacamp.com", "fireflies.ai"
}

def is_blocked_sender(sender: str) -> bool:
    sender_lower = sender.lower()
    return any(domain in sender_lower for domain in BLOCKED_DOMAINS)

def format_date(date_str: str) -> str:
    try:
        dt = parsedate_to_datetime(date_str)
        return dt.strftime("%d/%m/%Y")
    except:
        return date_str
    
def load_processed_ids():
    if os.path.exists("processed_ids.json"):
        with open("processed_ids.json", "r") as f:
            return set(json.load(f))
    return set()

def save_processed_id(email_id: str):
    ids = load_processed_ids()
    ids.add(email_id)
    with open("processed_ids.json", "w") as f:
        json.dump(list(ids), f)

def find_existing_row(company: str) -> int | None:
    all_rows = worksheet.get_all_values()
    for i, row in enumerate(all_rows[1:], start=2):  # skip header
        if row[0].strip().lower() == company.strip().lower():
            return i
    return None

def extract_job_info(subject: str, sender: str, body: str = "") -> dict:
    prompt = f"""
You are parsing job application emails. Extract structured info.

Email details:
- Subject: {subject}
- From: {sender}
- Body preview: {body[:500]}

Rules:
- "role" means the JOB TITLE (e.g. "Software Engineer Intern"), NOT a meeting name
- "company" is the hiring company. For calendar invites or meeting recaps, extract 
  company name from the subject line directly
- For subjects like "hyperionsoft interview: intro" → company: "Hyperionsoft", status: Interview
- status must be exactly one of: Applied, OA, Interview, Rejected, Offer, None
- "Thank you for applying" → status: Applied
- Rejection or "not moving forward" → status: Rejected
- OA/assessment/HackerRank invite → status: OA
- Interview scheduling, invite, confirmation, recap → status: Interview
- If unrelated to a direct job application → is_job_related: false
- If you cannot confidently determine status → status: None

Return ONLY this JSON, no extra text:
{{
  "is_job_related": true or false,
  "company": "hiring company name or empty string",
  "role": "job title or empty string",
  "status": "Applied | OA | Interview | Rejected | Offer | None"
}}
"""
    chat = groq_client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"}
    )
    return json.loads(chat.choices[0].message.content)

@app.get("/hello")
def hello():
    return {"message": "hi"}

# --- Google Sheets setup ---
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
gc = gspread.authorize(creds)

SHEET_URL = os.getenv("SHEET_URL")
worksheet = gc.open_by_url(SHEET_URL).sheet1

# --- Models ---
class Application(BaseModel):
    company: str
    role: str
    location: str = ""
    application_date: str = ""
    source: str = ""
    status: str = "Applied"
    comments: str = ""
    follow_up_date: str = ""
    notes: str = ""
    link: str = ""
    days_since_applied: str = ""

    @field_validator("company", "role")
    @classmethod
    def not_empty(cls, v):
        if not v.strip():
            raise ValueError("Field cannot be blank")
        return v

# --- Routes ---
@app.get("/applications")
def list_all():
    rows = worksheet.get_all_records()
    return {"applications": rows, "total": len(rows)}

@app.post("/applications")
def create(application: Application):
    row = [
        application.company,
        application.role,
        application.location,
        application.application_date,
        application.source,
        application.status,
        application.comments,
        application.follow_up_date,
        application.notes,
        application.link,
        application.days_since_applied,
    ]
    worksheet.append_row(row)
    return {"message": "Row added", "data": application}

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

def get_gmail_service():
    creds = None
    if os.path.exists("token.pickle"):
        with open("token.pickle", "rb") as token:
            creds = pickle.load(token)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                "gmail_credentials.json", GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        with open("token.pickle", "wb") as token:
            pickle.dump(creds, token)
    return build("gmail", "v1", credentials=creds)

@app.get("/emails/fetch")
def fetch_emails():
    service = get_gmail_service()

    results = service.users().messages().list(
        userId="me",
        q="subject:(applied OR interview OR assessment OR rejected OR offer) newer_than:30d",
        maxResults=10
    ).execute()

    messages = results.get("messages", [])
    emails = []

    for msg in messages:
        detail = service.users().messages().get(
            userId="me", id=msg["id"], format="metadata",
            metadataHeaders=["Subject", "From", "Date"]
        ).execute()

        headers = {h["name"]: h["value"] for h in detail["payload"]["headers"]}
        emails.append({
            "id": msg["id"],
            "subject": headers.get("Subject", ""),
            "from": headers.get("From", ""),
            "date": headers.get("Date", "")
        })

    return {"emails": emails, "count": len(emails)}

@app.get("/emails/process")
def process_emails():
    service = get_gmail_service()
    processed_ids = load_processed_ids()

    results = service.users().messages().list(
        userId="me",
        q="subject:(applied OR interview OR assessment OR rejected OR offer) newer_than:30d",
        maxResults=20
    ).execute()

    messages = results.get("messages", [])
    processed = []

    for msg in messages:
        if msg["id"] in processed_ids:
            continue

        detail = service.users().messages().get(
            userId="me", id=msg["id"], format="full"
        ).execute()

        headers = {h["name"]: h["value"] for h in detail["payload"]["headers"]}
        subject = headers.get("Subject", "")
        sender = headers.get("From", "")
        date = format_date(headers.get("Date", ""))

        body = ""
        parts = detail.get("payload", {}).get("parts", [])
        for part in parts:
            if part.get("mimeType") == "text/plain":
                data = part["body"].get("data", "")
                body = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="ignore")
                break
        
        if is_blocked_sender(sender):
            save_processed_id(msg["id"])
            continue

        extracted = extract_job_info(subject, sender, body)

        if not extracted.get("is_job_related"):
            save_processed_id(msg["id"])
            continue

        if extracted.get("status") == "None" or not extracted.get("company"):
            save_processed_id(msg["id"])
            continue

        company = extracted.get("company", "")
        role = extracted.get("role", "")
        status = extracted.get("status", "Applied")

        source = "Career Portal" if status == "Applied" else "Email"

        existing_row = find_existing_row(company)

        if existing_row:
            worksheet.update_cell(existing_row, 6, status)
            processed.append({**extracted, "action": "updated", "row": existing_row})
        else:
            row = [
                company,
                role,
                "",           # location
                date,         # DD/MM/YYYY
                source,       # Career Portal or Email
                status,
                "",           # comments
                "",           # follow-up date
                "",           # notes — intentionally blank
                "",           # link
                "",           # days since applied
            ]
            worksheet.append_row(row)
            processed.append({**extracted, "action": "added"})

        save_processed_id(msg["id"])

    return {"processed": processed, "total": len(processed)}

@app.get("/emails/debug")
def debug_emails():
    service = get_gmail_service()

    results = service.users().messages().list(
        userId="me",
        q="subject:(applied OR interview OR assessment OR rejected OR offer) newer_than:30d",
        maxResults=20
    ).execute()

    messages = results.get("messages", [])
    debug_output = []

    for msg in messages:
        detail = service.users().messages().get(
            userId="me", id=msg["id"], format="full"
        ).execute()

        headers = {h["name"]: h["value"] for h in detail["payload"]["headers"]}
        subject = headers.get("Subject", "")
        sender = headers.get("From", "")
        date = headers.get("Date", "")

        body = ""
        parts = detail.get("payload", {}).get("parts", [])
        for part in parts:
            if part.get("mimeType") == "text/plain":
                data = part["body"].get("data", "")
                body = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="ignore")
                break

        if is_blocked_sender(sender):
            save_processed_id(msg["id"])
            continue

        extracted = extract_job_info(subject, sender, body)

        debug_output.append({
            "subject": subject,
            "from": sender,
            "llm_decision": extracted
        })

    return {"emails": debug_output}
