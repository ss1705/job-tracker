import base64
import json
import time
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from . import config
from .parser import html_to_text

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

QUERY_TERMS = (
    '"your application" OR "thank you for applying" OR "thanks for applying" OR "application received" '
    'OR "received your application" OR "application update" OR "application status" OR "update on your" '
    'OR "next steps" OR "moving forward" OR "interview" OR "assessment" OR "unfortunately" OR "offer" '
    'OR "your candidacy" OR "your submission" OR "online submission" OR "application has been" '
    'OR "we have received" OR "we\'ve received" OR "screening" OR "invited to" OR "thank you for your interest"'
)


class AuthRequired(Exception):
    pass


def authorize(interactive: bool = False) -> Credentials:
    creds = None
    if config.GMAIL_TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(config.GMAIL_TOKEN_FILE), SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    elif interactive:
        flow = InstalledAppFlow.from_client_secrets_file(str(config.GMAIL_CLIENT_FILE), SCOPES)
        creds = flow.run_local_server(port=0)
    else:
        raise AuthRequired("Run `python -m jobtracker.gmail` once to sign in to Gmail.")
    config.GMAIL_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.GMAIL_TOKEN_FILE.write_text(creds.to_json())
    config.GMAIL_TOKEN_FILE.chmod(0o600)
    return creds


def service():
    return build("gmail", "v1", credentials=authorize(), cache_discovery=False)


def build_query(since: date, until: date | None = None) -> str:
    # Gmail evaluates after:/before: in Pacific time, so for US-east (or any other) users the first/last
    # hours of a day can fall outside the range. Pad by a day; already-seen emails are skipped anyway.
    since = since - timedelta(days=1)
    q = f"({QUERY_TERMS}) after:{since:%Y/%m/%d} -category:promotions -category:social -in:spam -in:trash"
    if until:
        q += f" before:{(until + timedelta(days=2)):%Y/%m/%d}"
    return q


def list_message_ids(svc, since: date, until: date | None = None) -> list[str]:
    ids, token = [], None
    while True:
        resp = svc.users().messages().list(
            userId="me", q=build_query(since, until), maxResults=500, pageToken=token
        ).execute()
        ids += [m["id"] for m in resp.get("messages", [])]
        token = resp.get("nextPageToken")
        if not token:
            return ids


def _walk(payload, plain: list, html: list):
    mime = payload.get("mimeType", "")
    data = payload.get("body", {}).get("data")
    if data and mime in ("text/plain", "text/html"):
        text = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="ignore")
        (plain if mime == "text/plain" else html).append(text)
    for part in payload.get("parts", []) or []:
        _walk(part, plain, html)


def extract_body(payload) -> str:
    plain, html = [], []
    _walk(payload, plain, html)
    text = "\n".join(plain).strip()
    if len(text) < 80 and html:  # HTML-only (or stub plain part): common for ATS mail
        text = html_to_text("\n".join(html))
    return text


_MIN_INTERVAL = 0.12   # ~8 requests/s keeps us well under Gmail's per-user units/minute quota
_last_call = 0.0


def _is_rate_limit(e: HttpError) -> bool:
    if e.resp.status == 429:
        return True
    if e.resp.status == 403:
        try:
            reasons = {d.get("reason") for d in json.loads(e.content)["error"].get("errors", [])}
        except Exception:
            return False
        return bool(reasons & {"rateLimitExceeded", "userRateLimitExceeded"})
    return False


def _get_with_backoff(svc, msg_id: str, on_wait=None) -> dict:
    global _last_call
    for attempt in range(8):
        wait = _MIN_INTERVAL - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()
        try:
            return svc.users().messages().get(userId="me", id=msg_id, format="full").execute()
        except HttpError as e:
            if not _is_rate_limit(e) or attempt == 7:
                raise
            delay = min(10 * 2 ** attempt, 65)  # quota is per-minute, so back off up to a full window
            if on_wait:
                on_wait(delay)
            time.sleep(delay)


def fetch_message(svc, msg_id: str, on_wait=None) -> dict:
    detail = _get_with_backoff(svc, msg_id, on_wait)
    headers = {h["name"].lower(): h["value"] for h in detail["payload"].get("headers", [])}
    try:
        received = parsedate_to_datetime(headers.get("date", "")).astimezone(timezone.utc)
    except Exception:
        received = datetime.fromtimestamp(int(detail["internalDate"]) / 1000, tz=timezone.utc)
    return {
        "id": msg_id,
        "subject": headers.get("subject", ""),
        "sender": headers.get("from", ""),
        "received_at": received.strftime("%Y-%m-%dT%H:%M:%S"),
        "body": extract_body(detail["payload"]),
    }


if __name__ == "__main__":
    authorize(interactive=True)
    print(f"Signed in. Token saved to {config.GMAIL_TOKEN_FILE}")
