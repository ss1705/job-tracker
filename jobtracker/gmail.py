import base64
import json
import re
import time
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from . import config
from .parser import ATS_DOMAINS, PLATFORM_DOMAINS, html_to_text

READ_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"      # only used to email the daily digest to yourself
SCOPES = [READ_SCOPE, SEND_SCOPE]                               # requested at sign-in

QUERY_TERMS = (
    '"your application" OR "thank you for applying" OR "thanks for applying" OR "application received" '
    'OR "received your application" OR "application update" OR "application status" OR "update on your" '
    'OR "next steps" OR "moving forward" OR "interview" OR "assessment" OR "unfortunately" OR "offer" '
    'OR "your candidacy" OR "your submission" OR "online submission" OR "application has been" '
    'OR "we have received" OR "we\'ve received" OR "screening" OR "invited to" OR "thank you for your interest" '
    'OR "job application" OR "candidate" OR "candidacy"'
)

# Recruiting-system senders are searched in EVERY Gmail tab: Gmail files some of them under Promotions or Social,
# which the keyword clause excludes, and real mail (screening invitations, portal links) was being missed that way.
# plus sender names that say "recruiting" (employers' own senders like recruiting@jobalerts.careers.hpe.com, filed under Promotions)
SENDER_NAMES = ("recruiting", "recruiter", "recruitment", "careers", "talent", "hiring")
SENDER_TERMS = " OR ".join(sorted({d for d in (*ATS_DOMAINS, *PLATFORM_DOMAINS) if d not in ("adp.com", "shl.com")} | set(SENDER_NAMES)))


def matched_core() -> str:
    """The Gmail clause for 'application mail': keywords outside Promotions/Social, OR a known recruiting-system sender anywhere."""
    return f"((({QUERY_TERMS}) -category:promotions -category:social) OR from:({SENDER_TERMS}))"



# bump when the search query changes, so evaluation reports say which version of the search they measured
#   1 = keywords outside Promotions/Social
#   2 = + recruiting-system senders and sender names in every tab, + "job application"/"candidate" wording, excludes your own mail
QUERY_VERSION = 2


class AuthRequired(Exception):
    pass


class SendPermissionMissing(Exception):
    pass


def authorize(interactive: bool = False) -> Credentials:
    """Credentials from the saved token. Reading mail only needs the read scope, so tokens created before the
    send scope existed keep working for syncing; the digest checks has_send_permission() separately."""
    creds = None
    if config.GMAIL_TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(config.GMAIL_TOKEN_FILE))   # scopes come from the token itself
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


def has_send_permission() -> bool:
    try:
        return SEND_SCOPE in (json.loads(config.GMAIL_TOKEN_FILE.read_text()).get("scopes") or [])
    except Exception:
        return False


def service():
    return build("gmail", "v1", credentials=authorize(), cache_discovery=False)


def my_address(svc) -> str:
    return svc.users().getProfile(userId="me").execute()["emailAddress"]


def send_to_self(subject: str, text: str, html: str) -> str:
    """Email the digest to your own address. Never sends to anyone else."""
    if not has_send_permission():
        raise SendPermissionMissing("Re-run `python -m jobtracker.gmail` to grant permission to email the digest.")
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    svc = service()
    me = my_address(svc)
    msg = MIMEMultipart("alternative")
    msg["To"], msg["From"], msg["Subject"] = me, me, subject
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    svc.users().messages().send(userId="me", body={"raw": raw}).execute()
    return me


def build_query(since: date, until: date | None = None) -> str:
    # Gmail evaluates after:/before: in Pacific time, so for US-east (or any other) users the first/last
    # hours of a day can fall outside the range. Pad by a day; already-seen emails are skipped anyway.
    since = since - timedelta(days=1)
    q = f"{matched_core()} after:{since:%Y/%m/%d} -from:me -in:spam -in:trash"
    if until:
        q += f" before:{(until + timedelta(days=2)):%Y/%m/%d}"
    return q


def fetch_headers(svc, ids: list[str], chunk: int = 20, on_progress=None) -> dict[str, dict]:
    """Subject + sender for many messages using batched requests (about 20x fewer round trips than one by one)."""
    out: dict[str, dict] = {}
    pending = list(ids)
    for _round in range(4):
        failed: list[str] = []

        def cb(request_id, resp, exc):
            if exc is not None or not resp:
                failed.append(request_id)
                return
            h = {x["name"].lower(): x["value"] for x in resp["payload"].get("headers", [])}
            out[resp["id"]] = {"subject": h.get("subject", ""), "sender": h.get("from", "")}

        for i in range(0, len(pending), chunk):
            batch = svc.new_batch_http_request(callback=cb)
            for mid in pending[i:i + chunk]:
                batch.add(svc.users().messages().get(userId="me", id=mid, format="metadata", metadataHeaders=["Subject", "From"]), request_id=mid)
            batch.execute()
            time.sleep(0.6)                                  # stay under Gmail's per-user quota units per second
            if on_progress:
                on_progress(len(out))
        if not failed:
            break
        pending = failed
        time.sleep(20)                                       # rate-limited ones: wait out the window, then retry just those
    return out


def list_linkedin_ids(svc, since: date, until: date | None = None) -> list[str]:
    """LinkedIn notifications about responses to your outreach. They arrive under Social/Updates, which the
    main application search excludes, so they get their own query."""
    q = (f'from:linkedin.com ("accepted your invitation" OR "just messaged you" OR "sent you a message") '
         f'after:{(since - timedelta(days=1)):%Y/%m/%d} -in:spam -in:trash')
    if until:
        q += f" before:{(until + timedelta(days=2)):%Y/%m/%d}"
    ids, token = [], None
    while True:
        resp = svc.users().messages().list(userId="me", q=q, maxResults=500, pageToken=token).execute()
        ids += [m["id"] for m in resp.get("messages", [])]
        token = resp.get("nextPageToken")
        if not token:
            return ids


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


_PROFILE_URL = re.compile(r"https?://[^\s\"'<>)]*linkedin\.com/(?:comm/)?in/[^\s\"'<>)]+", re.I)


def _html_text(payload) -> str:
    """Visible text of the HTML part(s). LinkedIn notices keep the person's name and headline only there."""
    plain, html = [], []
    _walk(payload, plain, html)
    return html_to_text("\n".join(html)) if html else ""


def extract_profile_links(payload) -> list[str]:
    """LinkedIn profile URLs in the raw message (the HTML-to-text step drops hrefs)."""
    plain, html = [], []
    _walk(payload, plain, html)
    seen, out = set(), []
    for u in _PROFILE_URL.findall("\n".join(plain + html)):
        u = u.replace("&amp;", "&")
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out[:30]


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
        "links": extract_profile_links(detail["payload"]),
        "html_text": _html_text(detail["payload"]),
    }


def sign_in() -> Credentials:
    """Always run the consent flow (used by `python -m jobtracker.gmail`), so new permissions can be granted
    even though the saved token is still valid."""
    flow = InstalledAppFlow.from_client_secrets_file(str(config.GMAIL_CLIENT_FILE), SCOPES)
    creds = flow.run_local_server(port=0)
    config.GMAIL_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.GMAIL_TOKEN_FILE.write_text(creds.to_json())
    config.GMAIL_TOKEN_FILE.chmod(0o600)
    return creds


if __name__ == "__main__":
    sign_in()
    print(f"Signed in. Token saved to {config.GMAIL_TOKEN_FILE}")
    print("Read access: yes | Email-the-digest permission:", "yes" if has_send_permission() else "NO (untick/denied on the consent screen?)")
