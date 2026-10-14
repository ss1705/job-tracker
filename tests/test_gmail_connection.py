"""An expired or revoked Gmail connection is a normal state to explain, never an internal server error."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from google.auth.exceptions import RefreshError

from test_milestones import env  # noqa: F401  (the app + temp database fixture)


class DeadCreds:
    valid = False
    expired = True
    refresh_token = "x"

    def refresh(self, request):
        raise RefreshError("invalid_grant: Token has been expired or revoked.")


@pytest.fixture()
def dead_connection(env, tmp_path, monkeypatch):
    c, db, m, sync = env
    from jobtracker import gmail
    token = tmp_path / "gmail_connection.json"
    token.write_text("{}")
    monkeypatch.setattr(gmail.config, "GMAIL_TOKEN_FILE", token)
    monkeypatch.setattr(gmail.Credentials, "from_authorized_user_file", staticmethod(lambda *a, **k: DeadCreds()))
    return c, db, gmail


def test_an_expired_connection_becomes_a_clear_message(dead_connection):
    c, db, gmail = dead_connection
    with pytest.raises(gmail.AuthRequired) as e:
        gmail.authorize()
    assert "expired or was revoked" in str(e.value) and "python -m jobtracker.gmail" in str(e.value)


def test_endpoints_answer_401_with_the_reason_not_500(dead_connection):
    c, db, gmail = dead_connection
    for method, path, body in (("post", "/api/milestones/rescan", {"days": 21}), ("post", "/api/sync", {}), ("post", "/api/eval/sample", {})):
        r = getattr(c, method)(path, json=body)
        assert r.status_code in (401, 404), (path, r.status_code)                  # 404 only when the evaluation tools are off
        if r.status_code == 401:
            assert "Reconnect it" in r.json()["detail"]


def test_the_page_can_ask_whether_gmail_is_connected_and_the_answer_is_cached(dead_connection, monkeypatch):
    c, db, gmail = dead_connection
    from jobtracker import api
    api._gmail_check.update(at=0.0, value=None)
    calls = []
    real = gmail.authorize
    monkeypatch.setattr(api.gmail, "authorize", lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    first = c.get("/api/gmail/status").json()
    assert first["connected"] is False and "Reconnect it" in first["message"]
    c.get("/api/gmail/status")
    assert len(calls) == 1                                                          # the second answer came from the cache

    api._gmail_check.update(at=0.0, value=None)
    monkeypatch.setattr(api.gmail, "authorize", lambda *a, **k: object())
    assert c.get("/api/gmail/status").json() == {"connected": True}

    api._gmail_check.update(at=0.0, value=None)

    def offline(*a, **k):
        raise ConnectionError("no network")
    monkeypatch.setattr(api.gmail, "authorize", offline)
    unknown = c.get("/api/gmail/status").json()
    assert unknown["connected"] is None and "ConnectionError" in unknown["message"]      # offline is 'unknown', not 'disconnected'
    api._gmail_check.update(at=0.0, value=None)


def test_background_sync_problems_are_recorded_and_back_off(env):
    c, db, m, sync = env
    from jobtracker import scheduler
    scheduler._record_sync_problem("Your Gmail connection has expired or was revoked. Reconnect it: ...")
    last = c.get("/api/sync/last").json()
    assert "expired or was revoked" in last["error"] and last["processed"] == 0                # the page can show it in red
    now = datetime.now(timezone.utc)
    stored = json.loads(json.dumps(last))
    assert not scheduler.is_due(stored, now + timedelta(minutes=30), 120)                       # backs off instead of retrying every minute
    assert scheduler.is_due(stored, now + timedelta(minutes=125), 120)


def test_the_digest_reports_a_dead_connection_plainly(dead_connection, monkeypatch):
    c, db, gmail = dead_connection
    from jobtracker import digest
    monkeypatch.setattr(digest, "collect", lambda today=None: {"subject": "s", "text": "t", "html": "<p>t</p>", "count": 1})
    monkeypatch.setattr(gmail, "has_send_permission", lambda: True)
    r = digest.send_now(test=False)
    assert not r["sent"] and r["error"].startswith("Your Gmail connection has expired") and not r["needs_permission"]
