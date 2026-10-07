import importlib

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    from jobtracker import config, db, sync
    importlib.reload(config); importlib.reload(db); importlib.reload(sync)
    db.init_db()
    return db, sync


def msg(i, subject, sender, body, ts):
    return {"id": i, "subject": subject, "sender": sender, "body": body, "received_at": ts}


def feed(db, sync, m):
    from jobtracker.parser import parse_email
    p = parse_email(m["subject"], m["sender"], m["body"], use_llm=False)
    with db.session() as c:
        return sync.apply_parsed(c, m, p)


def statuses(db):
    with db.session() as c:
        return [(r["company"], r["role"], r["status"]) for r in c.execute("SELECT * FROM applications ORDER BY id")]


def test_rejection_updates_existing_application(env):
    db, sync = env
    gh = "Acme <no-reply@us.greenhouse-mail.io>"
    feed(db, sync, msg("1", "Thank you for applying to Acme", gh, "Thanks for applying to the Backend Co-op position at Acme", "2026-09-01T10:00:00"))
    feed(db, sync, msg("2", "Thank you for applying to Acme", gh, "Thanks for applying to the ML Engineer New Grad position at Acme", "2026-09-02T10:00:00"))
    feed(db, sync, msg("3", "Acme Application Update", gh, "We have decided to pursue other candidates.", "2026-09-10T10:00:00"))
    assert any(s == "Rejected" for _, _, s in statuses(db))


def test_applied_receipt_after_interview_does_not_downgrade(env):
    db, sync = env
    s = sync.derive_status([("2026-09-01", "Applied"), ("2026-09-05", "Interview"), ("2026-09-06", "Applied")])
    assert s == "Interview"
    assert sync.derive_status([("2026-09-01", "Applied"), ("2026-09-05", "Rejected")]) == "Rejected"
    assert sync.derive_status([("1", "Interview"), ("2", "Offer"), ("3", "Update")]) == "Offer"


def test_idempotent_event_insert(env):
    db, sync = env
    m = msg("dup", "Thanks for applying", "Foo <no-reply@foo.com>", "Thank you for applying to Foo", "2026-09-01T10:00:00")
    feed(db, sync, m)
    with pytest.raises(Exception):
        feed(db, sync, m)  # UNIQUE(gmail_id) guards against double-counting


def test_roleless_reminder_attaches_instead_of_duplicating(env):
    db, sync = env
    gh = "Roblox <no-reply@roblox.com>"
    feed(db, sync, msg("a", "Thank you for applying to Roblox!", gh, "Thanks for applying for the Software Engineer, Early Career position", "2026-09-01T10:00:00"))
    feed(db, sync, msg("b", "Roblox Application: Reminder to Verify Your Email Address", gh, "application received", "2026-09-02T10:00:00"))
    assert len(statuses(db)) == 1


def test_oa_emails_from_platform_and_employer_collapse_to_one_record(env):
    db, sync = env
    from jobtracker.parser import Parsed
    # three emails about one assessment, as the parser would label them once the LLM reads the body
    emails = [
        ("1", "Maven Assessment Invitation", "Maven Securities <no-reply@mavensecurities.com>", "2026-09-01T09:00:00"),
        ("2", "Thanks for taking the Maven Securities test", "Maven Securities Hiring Team <support@hackerrank.com>", "2026-09-02T09:00:00"),
        ("3", "FYI For any assessments with Maven", "no-reply@mavensecurities.com", "2026-09-02T12:00:00"),
    ]
    names = ["Maven Securities", "Maven Securities", "Mavensecurities"]
    with db.session() as c:
        for (i, subj, snd, ts), name in zip(emails, names):
            sync.apply_parsed(c, msg(i, subj, snd, "online assessment", ts), Parsed(is_job=True, company=name, status="OA"))
    assert statuses(db) == [("Maven Securities", "", "OA")]


def test_company_key_ignores_spacing():
    from jobtracker.db import company_key
    assert company_key("Maven Securities") == company_key("Mavensecurities")
    assert company_key("Graybar Electric Company, Inc.") == company_key("graybar electric")


def test_late_reminders_do_not_reopen_a_rejection(env):
    db, sync = env
    ev = [("2026-08-01", "Applied"), ("2026-08-05", "OA"), ("2026-08-22", "Rejected"), ("2026-08-25", "OA"), ("2026-08-26", "Applied")]
    assert sync.derive_status(ev) == "Rejected"
    assert sync.derive_status(ev + [("2026-09-01", "Interview")]) == "Interview"


def test_company_name_variants_are_the_same_company():
    from jobtracker.db import same_company
    assert same_company("Maven", "Maven Securities")
    assert same_company("Alpha Omega", "Alpha Omega Integration")
    assert same_company("Hyperion", "Hyperion - Global AI Consulting Services")
    assert not same_company("Apple", "Applebee's")
    assert not same_company("Meta", "Metabolic Labs")


def test_applied_date_uses_local_timezone(env, monkeypatch):
    db, sync = env
    from jobtracker import config
    monkeypatch.setattr(config, "TIMEZONE", "America/Los_Angeles")
    assert sync.local_date("2026-10-07T02:40:21") == "2026-10-06"      # 7:40 PM PDT the day before
    assert sync.local_date("2026-10-07T15:00:00") == "2026-10-07"
    monkeypatch.setattr(config, "TIMEZONE", "Asia/Kolkata")
    assert sync.local_date("2026-10-07T02:40:21") == "2026-10-07"


def test_auto_sync_schedule():
    from datetime import datetime, timedelta, timezone
    from jobtracker.scheduler import is_due
    now = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
    ago = lambda m: (now - timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert is_due(None, now, 120)                                   # never synced
    assert not is_due({"at": ago(30)}, now, 120)
    assert is_due({"at": ago(121)}, now, 120)
    assert not is_due({"at": ago(30), "error": "Groq quota"}, now, 120)   # back off after a failure
    assert is_due({"at": ago(61), "error": "Groq quota"}, now, 30)
    assert not is_due({"at": ago(9999)}, now, 0)                    # disabled


def test_follow_up_rules():
    from datetime import date
    from jobtracker import followups
    today = date(2026, 10, 20)
    app = lambda **kw: {"status": "Applied", "applied_at": "2026-10-01", "followed_up_at": None, "follow_up_at": None, "deadline": None, "contact_type": "Referral", **kw}
    f = lambda a, resp=False, last=None: followups.flags(a, resp, last, today)

    assert f(app())["follow_up_due"]                                    # referral: 19 days, no response
    assert not f(app(contact_type=""))["follow_up_due"]                 # cold application: nobody to follow up with
    assert f(app(contact_type="", follow_up_at="2026-10-19"))["follow_up_due"]   # ...unless you set a reminder yourself
    assert not f(app(applied_at="2026-10-15"))["follow_up_due"]         # only 5 days
    assert not f(app(applied_at="2026-08-01"))["follow_up_due"]         # 80 days: ghosted, stop nagging
    assert not f(app(), resp=True)["follow_up_due"]                     # got some response
    assert not f(app(followed_up_at="2026-10-18"))["follow_up_due"]     # just followed up: clock restarted
    assert f(app(followed_up_at="2026-10-02"))["follow_up_due"]
    assert not f(app(follow_up_at="2026-10-27"))["follow_up_due"]       # snoozed
    assert f(app(follow_up_at="2026-10-19"))["follow_up_due"]           # manual reminder reached
    assert f(app(status="Interview"), last="2026-10-10")["follow_up_due"]       # silent for 10 days
    assert not f(app(status="Interview"), last="2026-10-18")["follow_up_due"]
    assert not f(app(status="Rejected", follow_up_at="2026-10-01"))["follow_up_due"]   # final states never nag
    assert f(app(status="Saved", deadline="2026-10-22", applied_at=None))["deadline_days"] == 2
    assert f(app(status="Saved", deadline="2026-10-18", applied_at=None))["deadline_days"] == -2


def test_roleless_receipts_on_different_days_stay_separate_and_can_be_split(env):
    db, sync = env
    from jobtracker.parser import Parsed
    hpe = "Hewlett Packard Enterprise <hpe@myworkday.com>"
    p = Parsed(is_job=True, company="Hewlett Packard Enterprise", status="Applied")
    with db.session() as c:
        sync.apply_parsed(c, msg("a", "Thank you for your online submission", hpe, "x", "2026-09-15T14:00:00"), p)
        sync.apply_parsed(c, msg("b", "Thank you for your online submission", hpe, "x", "2026-09-15T14:02:00"), p)   # same day duplicate
        sync.apply_parsed(c, msg("c", "Thank you for your online submission", hpe, "x", "2026-10-07T03:04:00"), p)   # different day
    with db.session() as c:
        rows = c.execute("SELECT id, needs_review, (SELECT COUNT(*) FROM events e WHERE e.application_id=a.id) n FROM applications a ORDER BY id").fetchall()
    assert [(r["needs_review"], r["n"]) for r in rows] == [(0, 2), (1, 1)]     # duplicate merged; later day flagged separately

    # manual un-merge of an existing merged record
    with db.session() as c:
        app_id = rows[0]["id"]
        ev = c.execute("SELECT id FROM events WHERE application_id=? ORDER BY received_at DESC", (app_id,)).fetchone()["id"]
        new_id = sync.split_events(c, app_id, [ev], "AI Engineer Graduate")
        assert new_id not in (rows[0]["id"], rows[1]["id"])
        with pytest.raises(ValueError):
            sync.split_events(c, new_id, [c.execute("SELECT id FROM events WHERE application_id=?", (new_id,)).fetchone()["id"]])  # can't empty it
