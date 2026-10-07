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


def test_contacts_flow_and_reminders(client):
    c, db, sync = client
    today = date.today()
    d = lambda n: (today + timedelta(days=n)).isoformat()

    app_id = c.post("/api/applications", json={"company": "Acme", "role": "SWE", "status": "Applied", "applied_at": d(-3)}).json()["id"]
    assert c.get("/api/applications").json()[0]["contact_type"] == ""

    # linking a referral contact switches on that application's reminders
    cid = c.post("/api/contacts", json={"name": "Priya", "company": "Acme", "kind": "Referral", "stage": "Reached out",
                                        "application_id": app_id}).json()["id"]
    a = c.get("/api/applications").json()[0]
    assert (a["contact_type"], a["contact_name"]) == ("Referral", "Priya")
    person = c.get("/api/contacts").json()[0]
    assert person["application_label"] == "Acme — SWE" and not person["follow_up_due"]    # reached out today

    # coffee chat happened yesterday, no thank-you yet -> nudge; "followed up" clears it
    c.patch(f"/api/contacts/{cid}", json={"stage": "Chatted", "chat_date": d(-2)})
    p = c.get("/api/contacts").json()[0]
    assert p["follow_up_due"] and "thank-you" in p["follow_up_reason"]
    assert c.get("/api/stats").json()["contacts_due"] == 1
    c.post(f"/api/contacts/{cid}/followed-up")
    assert not c.get("/api/contacts").json()[0]["follow_up_due"]

    # upcoming chat: heads-up the day before; snooze pushes the manual reminder out
    c.patch(f"/api/contacts/{cid}", json={"stage": "Chat scheduled", "chat_date": d(1), "last_contacted_at": None})
    assert "tomorrow" in c.get("/api/contacts").json()[0]["follow_up_reason"]
    assert c.post(f"/api/contacts/{cid}/snooze?days=3").json()["remind_on"] == d(3)
    assert not c.get("/api/contacts").json()[0]["follow_up_due"]

    # validation + deleting the application only unlinks the contact
    assert c.patch(f"/api/contacts/{cid}", json={"kind": "Nonsense"}).status_code == 422
    assert c.patch(f"/api/contacts/{cid}", json={"stage": "Nope"}).status_code == 422
    c.delete(f"/api/applications/{app_id}")
    assert c.get("/api/contacts").json()[0]["application_id"] is None


def test_contact_reach_out_rules():
    from jobtracker import followups
    today = date(2026, 10, 20)
    base = {"stage": "Reached out", "chat_date": None, "last_contacted_at": None, "remind_on": None, "created_at": "2026-10-10 12:00:00"}
    f = lambda **kw: followups.contact_flags({**base, **kw}, today)["due"]
    assert f()                                         # 10 days of silence
    assert not f(created_at="2026-10-18 09:00:00")     # only 2 days
    assert not f(created_at="2026-08-01 09:00:00")     # gone quiet, stop nagging
    assert not f(last_contacted_at="2026-10-17")       # nudged recently
    assert not f(stage="Closed", remind_on="2026-10-01")            # closed means done, stale reminder ignored
    assert f(stage="Referred", remind_on="2026-10-01")              # but a reminder you set after a referral counts
    assert not f(stage="Referred")
    assert f(stage="To reach out", remind_on="2026-10-19")
