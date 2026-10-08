import importlib
from datetime import date, timedelta

import pytest

TestClient = pytest.importorskip("fastapi.testclient").TestClient


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "m.db"))
    monkeypatch.setenv("AUTO_SYNC_MINUTES", "0")
    monkeypatch.setenv("DIGEST_ENABLED", "0")
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setenv("TIMEZONE", "America/Los_Angeles")
    from jobtracker import config, db, followups, sync, scheduler, contacts, drafts_api, evalset, eval_api, milestones, milestones_api, digest, api
    for m in (config, db, followups, sync, scheduler, contacts, drafts_api, evalset, eval_api, milestones, milestones_api, digest, api):
        importlib.reload(m)
    with TestClient(api.app) as c:
        yield c, db, milestones, sync


BODY = ("Hi Alex, thanks for applying. Please complete the HackerRank assessment by October 12, 2026 at 11:59 PM ET. "
        "The link expires after that. Good luck!")
RECEIVED = "2026-10-06T18:00:00"          # UTC; 11:00 AM in Los Angeles


def stub(monkeypatch, m, items):
    monkeypatch.setattr(m.llm, "chat_json", lambda *a, **k: {"items": items})


def test_extract_verifies_quote_converts_timezone_and_drops_bad_dates(env, monkeypatch):
    c, db, m, sync = env
    good = {"kind": "OA due", "when": "2026-10-12T23:59", "tz": "ET", "quote": "complete the HackerRank assessment by October 12, 2026 at 11:59 PM ET"}
    stub(monkeypatch, m, [
        good,
        {"kind": "OA due", "when": "2026-10-20", "tz": "", "quote": "this sentence is not in the email at all"},                       # invented quote
        {"kind": "Interview", "when": "2026-09-01", "tz": "", "quote": "Please complete the HackerRank assessment"},                  # in the past
        {"kind": "Party", "when": "2026-10-12", "tz": "", "quote": "Good luck"},                                                      # unknown kind
        {"kind": "Call", "when": "2027-09-01", "tz": "", "quote": "Good luck"}])                                                      # absurdly far
    out = m.extract("Your assessment", BODY, RECEIVED)
    assert len(out) == 1 and out[0]["kind"] == "OA due"
    assert out[0]["due_at"] == "2026-10-12T20:59"                 # 11:59 PM ET = 8:59 PM in Los Angeles
    assert out[0]["tz_note"] == "23:59 ET"


def test_relative_deadline_is_computed_from_the_emails_own_date(env, monkeypatch):
    c, db, m, sync = env
    stub(monkeypatch, m, [{"kind": "OA due", "when": None, "tz": "", "within_days": 7, "quote": "within 7 days"}])
    out = m.extract("Assessment", "Please complete the assessment within 7 days of receiving this email.", RECEIVED)
    assert out[0]["due_at"] == "2026-10-13"                       # received Oct 6 (local) + 7 days, no guessing by the model
    # nothing date-like in the email: no LLM call, no result
    monkeypatch.setattr(m.llm, "chat_json", lambda *a, **k: pytest.fail("should not call the model"))
    assert m.extract("Hello", "Thanks for your interest in our company.", RECEIVED) == []


def test_store_updates_on_reschedule_and_ignores_older_emails(env):
    c, db, m, sync = env
    with db.session() as conn:
        conn.execute("INSERT INTO applications (company, company_key, role) VALUES ('Acme','acme','SWE')")
        item = lambda due: {"kind": "Interview", "due_at": due, "quote": "x" * 8, "tz_note": ""}
        assert m.store(conn, 1, [item("2026-10-14T14:00")], "2026-10-06T10:00:00") == 1
        assert m.store(conn, 1, [item("2026-10-14T14:00")], "2026-10-07T10:00:00") == 0         # same time again: nothing to do
        assert m.store(conn, 1, [item("2026-10-15T10:00")], "2026-10-08T10:00:00") == 1         # a later email moves it
        assert m.store(conn, 1, [item("2026-10-20T10:00")], "2026-10-01T10:00:00") == 0         # an OLDER email must not win
        rows = conn.execute("SELECT due_at FROM milestones").fetchall()
    assert [r["due_at"] for r in rows] == ["2026-10-15T10:00"]


def test_api_flow_badges_digest_and_ics(env):
    c, db, m, sync = env
    app_id = c.post("/api/applications", json={"company": "Acme", "role": "SWE", "status": "OA"}).json()["id"]
    today = date.today()
    soon = (today + timedelta(days=1)).isoformat() + "T23:59"
    mid = c.post(f"/api/applications/{app_id}/milestones", json={"kind": "OA due", "due_at": soon, "note": "HackerRank"}).json()["id"]
    assert c.post(f"/api/applications/{app_id}/milestones", json={"kind": "Nope", "due_at": soon}).status_code == 422
    assert c.post(f"/api/applications/{app_id}/milestones", json={"kind": "Call", "due_at": "next tuesday"}).status_code == 422

    a = c.get("/api/applications").json()[0]
    assert a["next_milestone"]["kind"] == "OA due" and a["next_milestone"]["days"] == 1 and "tomorrow 11:59 PM" in a["next_milestone"]["when"]
    assert c.get("/api/milestones").json()[0]["company"] == "Acme"

    from jobtracker import digest
    d = digest.collect()
    assert d and "1 coming up" in d["subject"] and "OA due: Acme (SWE) — tomorrow 11:59 PM" in d["text"]

    ics = c.get(f"/api/milestones/{mid}.ics")
    assert ics.headers["content-type"].startswith("text/calendar")
    assert "SUMMARY:OA due: Acme (SWE)" in ics.text and "TRIGGER:-P1D" in ics.text and "DTSTART:" in ics.text

    c.patch(f"/api/milestones/{mid}", json={"done": True})                                # done: out of the digest, badges and default list
    assert c.get("/api/applications").json()[0]["next_milestone"] is None and c.get("/api/milestones").json() == []
    assert digest.collect() is None
    assert len(c.get("/api/milestones?include_done=true").json()) == 1
    c.delete(f"/api/milestones/{mid}")
    assert c.get("/api/milestones?include_done=true").json() == []


def test_sync_captures_milestones_for_oa_emails(env, monkeypatch):
    c, db, m, sync = env
    from jobtracker.parser import Parsed
    stub(monkeypatch, m, [{"kind": "OA due", "when": "2026-10-12T23:59", "tz": "ET",
                           "quote": "complete the HackerRank assessment by October 12, 2026 at 11:59 PM ET"}])
    msg = {"id": "g1", "subject": "Your assessment", "sender": "Acme <no-reply@acme.com>", "body": BODY, "received_at": RECEIVED}
    landed = {}
    with db.session() as conn:
        sync.apply_parsed(conn, msg, Parsed(is_job=True, company="Acme", role="SWE", status="OA"), out=landed)
    assert landed["app_id"] and sync.capture_milestones(landed["app_id"], msg) == 1
    with db.session() as conn:
        assert conn.execute("SELECT due_at FROM milestones").fetchone()[0] == "2026-10-12T20:59"


def test_time_limit_phrasings_reach_the_model(env, monkeypatch):
    c, db, m, sync = env
    seen = []
    monkeypatch.setattr(m.llm, "chat_json", lambda *a, **k: seen.append(1) or {"items": []})
    for text in ("You have 7 days to complete the test.", "This link is valid for 48 hours.", "Your invitation expires soon.",
                 "Please finish within 2 business days."):
        m.extract("Your assessment", text, RECEIVED)
    assert len(seen) == 4
    monkeypatch.setattr(m.llm, "chat_json", lambda *a, **k: pytest.fail("no date or time limit in this email"))
    assert m.extract("Thanks for taking the test", "Thanks for completing the assessment. We will be in touch.", RECEIVED) == []
