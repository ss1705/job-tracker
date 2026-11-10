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
        ("1", "Northwind Assessment Invitation", "Northwind Securities <no-reply@northwindsecurities.com>", "2026-09-01T09:00:00"),
        ("2", "Thanks for taking the Northwind Securities test", "Northwind Securities Hiring Team <support@hackerrank.com>", "2026-09-02T09:00:00"),
        ("3", "FYI For any assessments with Northwind", "no-reply@northwindsecurities.com", "2026-09-02T12:00:00"),
    ]
    names = ["Northwind Securities", "Northwind Securities", "Northwindsecurities"]
    with db.session() as c:
        for (i, subj, snd, ts), name in zip(emails, names):
            sync.apply_parsed(c, msg(i, subj, snd, "online assessment", ts), Parsed(is_job=True, company=name, status="OA"))
    assert statuses(db) == [("Northwind Securities", "", "OA")]


def test_late_reminders_do_not_reopen_a_rejection(env):
    db, sync = env
    ev = [("2026-08-01", "Applied"), ("2026-08-05", "OA"), ("2026-08-22", "Rejected"), ("2026-08-25", "OA"), ("2026-08-26", "Applied")]
    assert sync.derive_status(ev) == "Rejected"
    assert sync.derive_status(ev + [("2026-09-01", "Interview")]) == "Interview"


def test_company_name_variants_are_the_same_company():
    from jobtracker.db import company_key, same_company
    assert company_key("Northwind Securities") == company_key("Northwindsecurities")
    assert company_key("Graybar Electric Company, Inc.") == company_key("graybar electric")
    assert same_company("Northwind", "Northwind Securities")
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


