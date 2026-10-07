import importlib
from datetime import date, timedelta

import pytest

TestClient = pytest.importorskip("fastapi.testclient").TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "api.db"))
    monkeypatch.setenv("AUTO_SYNC_MINUTES", "0")      # never start the background sync in tests
    from jobtracker import config, db, followups, sync, scheduler, api
    for m in (config, db, followups, sync, scheduler, api):
        importlib.reload(m)
    with TestClient(api.app) as c:
        yield c, db, sync


def iso(days):
    return (date.today() + timedelta(days=days)).isoformat()


def test_saved_posting_flow(client):
    c, db, sync = client
    r = c.post("/api/applications", json={"company": "Acme", "role": "Backend Co-op", "status": "Saved", "deadline": iso(2)})
    app_id = r.json()["id"]
    a = c.get("/api/applications").json()[0]
    assert a["status"] == "Saved" and a["deadline_days"] == 2 and not a["follow_up_due"]
    assert c.get("/api/stats").json()["deadline_soon"] == 1

    # user clicks "Mark applied" (no emails yet): becomes Applied today and stays email-updatable
    c.patch(f"/api/applications/{app_id}", json={"status": "Applied"})
    a = c.get("/api/applications").json()[0]
    assert a["status"] == "Applied" and a["applied_at"] == date.today().isoformat() and a["status_locked"] == 0

    # the confirmation, then a rejection, arrive by email and attach to the same row
    from jobtracker.parser import Parsed
    with db.session() as conn:
        sync.apply_parsed(conn, {"id": "m1", "subject": "Thanks for applying", "sender": "Acme <no-reply@acme.com>", "body": "x",
                                 "received_at": "2026-09-01T10:00:00"}, Parsed(is_job=True, company="Acme", role="Backend Co-op", status="Applied"))
        sync.apply_parsed(conn, {"id": "m2", "subject": "Update", "sender": "Acme <no-reply@acme.com>", "body": "x",
                                 "received_at": "2026-09-05T10:00:00"}, Parsed(is_job=True, company="Acme", role="", status="Rejected"))
    apps = c.get("/api/applications").json()
    assert len(apps) == 1 and apps[0]["status"] == "Rejected"


def test_followed_up_and_snooze(client):
    c, db, sync = client
    old = (date.today() - timedelta(days=20)).isoformat()
    app_id = c.post("/api/applications", json={"company": "Beta", "role": "SWE", "status": "Applied", "applied_at": old}).json()["id"]
    assert not c.get("/api/applications").json()[0]["follow_up_due"]      # cold application: no automatic reminder
    c.patch(f"/api/applications/{app_id}", json={"contact_type": "Referral", "contact_name": "Priya"})
    assert c.get("/api/applications").json()[0]["follow_up_due"]          # with a referral it is flagged
    assert c.patch(f"/api/applications/{app_id}", json={"contact_type": "Nonsense"}).status_code == 422
    assert c.get("/api/stats").json()["follow_up"] == 1

    c.post(f"/api/applications/{app_id}/followed-up")
    assert not c.get("/api/applications").json()[0]["follow_up_due"]

    # a manual reminder in the past is due; snoozing pushes it a week out
    c.patch(f"/api/applications/{app_id}", json={"follow_up_at": iso(-1)})
    assert c.get("/api/applications").json()[0]["follow_up_due"]
    assert c.post(f"/api/applications/{app_id}/snooze?days=7").json()["follow_up_at"] == iso(7)
    assert not c.get("/api/applications").json()[0]["follow_up_due"]
