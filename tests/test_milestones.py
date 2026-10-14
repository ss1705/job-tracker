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


def test_action_emails_flag_the_application_until_resolved(env):
    c, db, m, sync = env
    from jobtracker.parser import Parsed
    mk = lambda i, subj, body, ts: {"id": i, "subject": subj, "sender": "Acme <no-reply@acme.com>", "body": body, "received_at": ts}
    with db.session() as conn:
        sync.apply_parsed(conn, mk("a", "Thanks for applying", "Thanks for applying to the SWE role.", "2026-10-01T18:00:00"),
                          Parsed(is_job=True, company="Acme", role="SWE", status="Applied"))
        sync.apply_parsed(conn, mk("b", "Additional info needed", "Please complete the attached form so we can process your application.", "2026-10-03T18:00:00"),
                          Parsed(is_job=True, company="Acme", role="SWE", status="Action"))
    a = c.get("/api/applications").json()[0]
    assert a["status"] == "Applied" and a["action_open"] and "complete the attached form" in a["action_note"]   # status is NOT changed
    assert c.get("/api/stats").json()["actions_open"] == 1

    from jobtracker import digest
    d = digest.collect()
    assert d and "1 needs your input" in d["subject"] and "NEEDS YOUR INPUT" in d["text"] and "Acme (SWE): Please complete the attached form" in d["text"]

    # the application is created as 'Applied' even when the first email we see is an action request
    with db.session() as conn:
        sync.apply_parsed(conn, mk("c", "Verify your email", "Please verify your email address.", "2026-10-04T18:00:00"),
                          Parsed(is_job=True, company="Zeta", status="Action"))
    zeta = next(x for x in c.get("/api/applications").json() if x["company"] == "Zeta")
    assert zeta["status"] == "Applied" and zeta["action_open"]

    # a later email showing progress resolves it; so does marking it done
    with db.session() as conn:
        sync.apply_parsed(conn, mk("d", "Your assessment", "Complete the assessment.", "2026-10-06T18:00:00"),
                          Parsed(is_job=True, company="Acme", role="SWE", status="OA"))
    acme = next(x for x in c.get("/api/applications").json() if x["company"] == "Acme")
    assert acme["status"] == "OA" and not acme["action_open"]
    assert c.post(f"/api/applications/{zeta['id']}/action-done").status_code == 200
    assert c.get("/api/stats").json()["actions_open"] == 0

    # flag by hand (for requests the parser never saw)
    assert c.post(f"/api/applications/{acme['id']}/action", json={"note": "Send transcript"}).status_code == 200
    again = next(x for x in c.get("/api/applications").json() if x["company"] == "Acme")
    assert again["action_open"] and again["action_note"] == "Send transcript"
    assert c.post("/api/applications/999/action", json={}).status_code == 404


# ---------------------------------------------------------------- scan outcomes, tolerant verification, retry

def _stub_llm(monkeypatch, m, response):
    monkeypatch.setattr(m.llm, "chat_json", response if callable(response) else (lambda *a, **k: response))


def test_quote_check_tolerates_entities_curly_quotes_and_dashes(env, monkeypatch):
    c, db, m, sync = env
    body = ("Hi&nbsp;Alex, you’ll have until October&nbsp;12, 2026 – 11:59&nbsp;PM ET to finish the test. "
            "The link expires after that.")
    item = {"kind": "OA due", "when": "2026-10-12T23:59", "tz": "ET",
            "quote": "you'll have until October 12, 2026 - 11:59 PM ET to finish the test"}        # straight quote, plain spaces, hyphen
    _stub_llm(monkeypatch, m, {"items": [item]})
    res = m.extract_detailed("Your assessment", body, RECEIVED)
    assert res["outcome"] == "found" and res["items"][0]["due_at"] == "2026-10-12T20:59"
    # a quote that is genuinely not in the email is still refused, and the reason is recorded
    _stub_llm(monkeypatch, m, {"items": [{**item, "quote": "you have until the end of the month to finish the test"}]})
    res = m.extract_detailed("Your assessment", body, RECEIVED)
    assert res["items"] == [] and res["outcome"] == "rejected" and "quote not found" in res["detail"]


def test_every_outcome_is_reported_not_silent(env, monkeypatch):
    c, db, m, sync = env
    vague = "The deadline for the assessment is the end of the month."            # no 'within N days' phrase: only the model can read it
    _stub_llm(monkeypatch, m, {"items": [{"kind": "OA due", "when": "2026-10-30", "tz": "", "quote": "The deadline for the assessment is the end of the month"}]})
    assert m.extract_detailed("Test", vague, RECEIVED)["outcome"] == "found"
    _stub_llm(monkeypatch, m, {"items": []})
    assert m.extract_detailed("Test", vague, RECEIVED)["outcome"] == "no_dates_found"
    _stub_llm(monkeypatch, m, None)                                                              # model returned nothing usable
    assert m.extract_detailed("Test", vague, RECEIVED)["outcome"] == "llm_failed"

    def exhausted(*a, **k):
        raise m.llm.LLMExhausted("quota")
    _stub_llm(monkeypatch, m, exhausted)
    assert m.extract_detailed("Test", vague, RECEIVED)["outcome"] == "llm_failed"
    monkeypatch.setattr(m.llm, "chat_json", lambda *a, **k: pytest.fail("no date wording: the model should not be called"))
    assert m.extract_detailed("Hello", "Thanks for your interest in our company.", RECEIVED)["outcome"] == "no_date_wording"
    monkeypatch.setattr(m.llm, "available", lambda: False)
    assert m.extract_detailed("Test", vague, RECEIVED)["outcome"] == "no_llm"
    _stub_llm(monkeypatch, m, {"items": [{"kind": "OA due", "when": "2020-01-01", "tz": "", "quote": "The deadline for the assessment is the end of the month"}]})
    monkeypatch.setattr(m.llm, "available", lambda: True)
    assert "out of range" in m.extract_detailed("Test", vague, RECEIVED)["detail"]               # a past date is refused with a reason


def test_failed_scans_are_recorded_explained_in_the_api_and_retried(env, monkeypatch):
    c, db, m, sync = env
    from jobtracker.parser import Parsed
    msg = {"id": "mx1", "subject": "Your assessment", "sender": "Maven <no-reply@maven.com>", "body": "The deadline for this test is the end of the month.",
           "received_at": RECEIVED}
    landed = {}
    with db.session() as conn:
        sync.apply_parsed(conn, msg, Parsed(is_job=True, company="Maven", role="Grad Dev", status="OA"), out=landed)
    app_id = landed["app_id"]

    _stub_llm(monkeypatch, m, None)                                                              # the model is down during the sync
    assert sync.capture_milestones(app_id, msg) == 0
    with db.session() as conn:
        assert conn.execute("SELECT outcome FROM milestone_scans WHERE gmail_id='mx1'").fetchone()[0] == "llm_failed"
    a = c.get("/api/applications").json()[0]
    assert a["milestone_scan"]["outcome"] == "llm_failed" and a["next_milestone"] is None       # the UI can say WHY there is no date

    # the model is back: the next sync's retry pass finds the deadline without anyone clicking anything
    _stub_llm(monkeypatch, m, {"items": [{"kind": "OA due", "when": "2026-10-30", "tz": "", "quote": "The deadline for this test is the end of the month"}]})
    monkeypatch.setattr(sync.gmail, "fetch_message", lambda svc, gid: msg)
    assert sync.retry_failed_scans(object()) == 1
    a = c.get("/api/applications").json()[0]
    assert a["next_milestone"]["kind"] == "OA due" and a["milestone_scan"] is None
    with db.session() as conn:
        assert conn.execute("SELECT outcome FROM milestone_scans WHERE gmail_id='mx1'").fetchone()[0] == "found"
    assert sync.retry_failed_scans(object()) == 0                                                # nothing left to retry


# ---------------------------------------------------------------- the plain rule for "within N days" and the fuller-text choice

@pytest.mark.parametrize("line, due, kind", [
    ("Please note that this test must be completed within 7 days of receiving this email.", "2026-10-13", "OA due"),
    ("Kindly complete the test within seven (7) days.", "2026-10-13", "OA due"),
    ("You have 7 days to complete the assessment.", "2026-10-13", "OA due"),
    ("The test link is valid for 7 days.", "2026-10-13", "OA due"),
    ("Please submit the requested documents within 2 weeks.", "2026-10-20", "Action due"),
    ("Please complete the coding challenge within 3 business days.", "2026-10-09", "OA due"),       # received Tue Oct 6 -> Fri Oct 9
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


def test_hours_deadline_and_merging_with_the_models_answer(env, monkeypatch):
    c, db, m, sync = env
    out = m.rule_deadlines("This link is valid for 48 hours. Please finish the test before then.", RECEIVED)
    assert out[0]["due_at"] == "2026-10-08T11:00"                                                 # 11:00 AM local on Oct 6 + 48 hours
    # model and rule agree on the same date: one item, not two
    _stub_llm(monkeypatch, m, {"items": [{"kind": "OA due", "when": None, "within_days": 7, "quote": "must be completed within 7 days"}]})
    res = m.extract_detailed("Test", "This assessment must be completed within 7 days.", RECEIVED)
    assert len(res["items"]) == 1 and "model + rule" in res["detail"]


def test_the_fuller_of_plain_and_html_text_is_used(env, monkeypatch):
    c, db, m, sync = env
    monkeypatch.setattr(m.llm, "available", lambda: False)
    footer_only = "You received this email because you applied. Manage your email preferences or unsubscribe at any time."
    real_message = footer_only + " Please complete the assessment within 7 days of receiving this email. " + "Good luck with it. " * 8
    assert m.extract_detailed("Your test", footer_only, RECEIVED)["items"] == []                  # plain part alone: the sentence is missing
    res = m.extract_detailed("Your test", footer_only, RECEIVED, html_text=real_message)
    assert res["outcome"] == "found" and res["items"][0]["due_at"] == "2026-10-13"
