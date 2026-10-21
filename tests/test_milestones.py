import importlib
from datetime import date

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


# ---------------------------------------------------------------- scan outcomes, tolerant verification, retry


# ---------------------------------------------------------------- the plain rule for "within N days" and the fuller-text choice

@pytest.mark.parametrize("line, due, kind", [
    ("Please note that this test must be completed within 7 days of receiving this email.", "2026-10-13", "OA due"),
    ("Kindly complete the test within seven (7) days.", "2026-10-13", "OA due"),
    ("You have 7 days to complete the assessment.", "2026-10-13", "OA due"),
    ("The test link is valid for 7 days.", "2026-10-13", "OA due"),
    ("Please submit the requested documents within 2 weeks.", "2026-10-20", "Action due"),
    ("Please complete the coding challenge within 3 business days.", "2026-10-09", "OA due"),       # received Tue Oct 6 -> Fri Oct 9
    ("You will have a 7-day window to complete the assessment.", "2026-10-13", "OA due"),
    ("Please complete the assessment within 7 calendar days.", "2026-10-13", "OA due"),
    ("The test must be completed within a week.", "2026-10-13", "OA due"),
    ("You have one week to finish the coding challenge.", "2026-10-13", "OA due"),
])
def test_rule_finds_relative_deadlines_without_any_model(env, monkeypatch, line, due, kind):
    c, db, m, sync = env
    monkeypatch.setattr(m.llm, "available", lambda: False)                                        # no model at all
    monkeypatch.setattr(m.llm, "chat_json", lambda *a, **k: pytest.fail("the rule alone should find this"))
    res = m.extract_detailed("Your assessment", f"Dear Candidate, welcome. {line} Good luck.", RECEIVED)
    assert res["outcome"] == "found" and "rule" in res["detail"]
    assert (res["items"][0]["due_at"], res["items"][0]["kind"]) == (due, kind)
    assert line.split(".")[0][:20] in res["items"][0]["quote"] or "7" in res["items"][0]["quote"] or "2 weeks" in res["items"][0]["quote"]


def test_rule_ignores_sentences_that_are_not_your_deadline(env, monkeypatch):
    c, db, m, sync = env
    monkeypatch.setattr(m.llm, "available", lambda: False)
    for line in ("We will get back to you within 7 days.", "You will hear from us within 5 business days.",
                 "Our team will review your results within 3 days.", "Your account was created 7 days ago.",
                 "Applications received in the last 7 days are being reviewed."):
        assert m.rule_deadlines(f"Hello. {line}", RECEIVED) == [], line
