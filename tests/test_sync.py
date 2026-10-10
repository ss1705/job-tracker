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


def test_digest_schedule_and_content():
    from datetime import date, datetime, timezone
    from jobtracker import digest
    local = lambda h, d=7: datetime(2026, 10, d, h, 30).astimezone()
    today = local(9).date().isoformat()
    ago = lambda m: (datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert not digest.is_due(local(7), None, 8)                                       # before 8am
    assert digest.is_due(local(8), None, 8)                                           # first check after 8am
    assert not digest.is_due(local(9), {"date": today, "sent": True, "at": ago(0)}, 8)
    assert not digest.is_due(local(9), {"date": today, "skipped": True, "at": ago(0)}, 8)
    assert not digest.is_due(local(9), {"date": today, "needs_permission": True, "at": ago(0)}, 8)
    assert not digest.is_due(local(9), {"date": today, "error": "boom", "at": ago(0)}, 8)   # failed just now: wait before retrying
    assert digest.is_due(local(9, 8), {"date": today, "sent": True, "at": ago(0)}, 8)       # next day

    assert digest.build([], [], [], [], date(2026, 10, 7)) is None                    # nothing to say, no email
    d = digest.build(
        [{"company": "Stripe", "role": "SWE", "follow_up_due": True, "follow_up_reason": "Applied 8 days ago, no response",
          "contact_type": "Referral", "contact_name": "Priya", "deadline_days": None},
         {"company": "Figma", "role": "", "follow_up_due": False, "follow_up_reason": "", "contact_type": "", "contact_name": "", "deadline_days": -2}],
        [{"name": "Marcus", "company": "Datadog", "follow_up_due": True, "follow_up_reason": "Chatted 2 days ago: send a thank-you / follow-up"}],
        [{"company": "Ramp", "role": "SWE", "type": "Interview", "received_at": "x"}],
        [{"name": "Tom", "company": "Notion", "response_kind": "Accepted invite", "responded_at": "x"}],
        date(2026, 10, 7))
    assert d["count"] == 5 and "1 deadline" in d["subject"] and "2 new responses" in d["subject"] and "2 to follow up" in d["subject"]
    assert "Interview: Ramp" in d["text"] and "overdue by 2 days" in d["text"] and "via Priya" in d["text"]
    assert "Tom (Notion) accepted invite on LinkedIn" in d["text"] and "<script" not in d["html"]


def test_outreach_records_and_responses(env):
    db, sync = env
    from jobtracker import outreach
    with db.session() as c:
        cid, created = outreach.record_outreach(c, name="Anurag Sharma", slug="anurag-sharma-1a2b", sent_at="2026-09-20", company="Stripe")
        assert created
        # same person again (matched by name, no slug) only enriches; earliest outreach date wins
        cid2, created2 = outreach.record_outreach(c, name="anurag  sharma", sent_at="2026-09-18", title="SWE")
        assert (cid2, created2) == (cid, False)
        row = c.execute("SELECT * FROM contacts WHERE id=?", (cid,)).fetchone()
        assert (row["outreach_sent_at"], row["title"], row["stage"], row["ext_key"]) == ("2026-09-18", "SWE", "Reached out", "anurag-sharma-1a2b")

        # accepted invite -> Replied, with when; replaying the same notice changes nothing
        assert outreach.record_response(c, name="Anurag Sharma", when="2026-09-25", kind="Accepted invite")[1] == "updated"
        assert outreach.record_response(c, name="Anurag Sharma", when="2026-09-25", kind="Accepted invite")[1] == "unchanged"
        row = c.execute("SELECT * FROM contacts WHERE id=?", (cid,)).fetchone()
        assert (row["stage"], row["responded_at"], row["response_kind"]) == ("Replied", "2026-09-25", "Accepted invite")

        # a later reply keeps the stage (never downgrades) and moves the response date
        c.execute("UPDATE contacts SET stage='Chat scheduled' WHERE id=?", (cid,))
        assert outreach.record_response(c, name="Anurag Sharma", when="2026-09-30", kind="Replied")[1] == "updated"
        assert c.execute("SELECT stage, response_kind FROM contacts WHERE id=?", (cid,)).fetchone()[:] == ("Chat scheduled", "Replied")

        # strangers: inbound messages are ignored, but an accepted invite can only be from someone you invited
        assert outreach.record_response(c, name="Random Recruiter", kind="Replied") == (None, "ignored")
        new_id, how = outreach.record_response(c, name="Priya Raman", when="2026-10-01", kind="Accepted invite", create_missing=True)
        assert how == "created" and c.execute("SELECT stage, source FROM contacts WHERE id=?", (new_id,)).fetchone()[:] == ("Replied", "linkedin-email")


def test_linkedin_email_recognition():
    from jobtracker import outreach
    base = {"sender": "Anurag via LinkedIn <invitations@linkedin.com>", "received_at": "2026-10-02T10:00:00",
            "links": ["https://www.linkedin.com/comm/in/someone-else-9z8y?trk=x", "https://www.linkedin.com/comm/in/anurag-sharma-1a2b?midToken=1"]}
    r = outreach.parse_linkedin_email({**base, "subject": "Anurag Sharma accepted your invitation, explore their network"})
    assert (r["name"], r["kind"], r["slug"]) == ("Anurag Sharma", "Accepted invite", "anurag-sharma-1a2b")
    r = outreach.parse_linkedin_email({**base, "sender": "LinkedIn <messaging-digest-noreply@linkedin.com>", "subject": "Anurag just messaged you"})
    assert (r["name"], r["kind"]) == ("Anurag", "Replied")
    assert outreach.parse_linkedin_email({**base, "subject": "I want to connect"}) is None                  # incoming request
    assert outreach.parse_linkedin_email({**base, "subject": "116 people viewed your profile"}) is None
    assert outreach.parse_linkedin_email({**base, "sender": "x@gmail.com", "subject": "Bob just messaged you"}) is None


def test_first_name_only_emails_match_by_profile_not_by_name(env):
    db, sync = env
    from jobtracker import outreach
    with db.session() as c:
        outreach.record_outreach(c, name="Anurag Sharma", slug="anurag-sharma-1a2b", sent_at="2026-09-01")
        # a different Anurag accepts: same first name, different profile -> a new contact, not Anurag Sharma
        cid, how = outreach.record_response(c, name="Anurag", slug="anurag-shete-053294178", when="2026-09-24",
                                            kind="Accepted invite", create_missing=True)
        assert how == "created"
        assert c.execute("SELECT name FROM contacts WHERE id=?", (cid,)).fetchone()[0] == "Anurag Shete"
        # his later reply matches by slug
        assert outreach.record_response(c, name="Anurag", slug="anurag-shete-053294178", when="2026-09-25", kind="Replied")[0] == cid
        # a bare first name with no profile link and two Anurags is ambiguous: ignored, never guessed
        assert outreach.record_response(c, name="Anurag", when="2026-09-26", kind="Replied") == (None, "ignored")
        assert outreach.display_name("Vishwa", "vishwak2003") == "Vishwa"             # unsplittable slug: keep the given name


LI_INVITES = """From,To,Sent At,Message,Direction,inviterProfileUrl,inviteeProfileUrl
Me Myself,Anurag Shete,"9/20/26, 8:12 PM",Hi Anurag! Love your work.,OUTGOING,https://www.linkedin.com/in/me,https://www.linkedin.com/in/anurag-shete-053294178
Me Myself,Priya Raman,"9/22/26, 9:00 AM",,OUTGOING,https://www.linkedin.com/in/me,https://www.linkedin.com/in/priya-raman-77
Some Stranger,Me Myself,"9/23/26, 9:00 AM",,INCOMING,https://www.linkedin.com/in/stranger,https://www.linkedin.com/in/me
"""
LI_CONNECTIONS = """Notes:
"When exporting your connection data, you may notice that some of the email addresses are missing."

First Name,Last Name,URL,Email Address,Company,Position,Connected On
Anurag,Shete,https://www.linkedin.com/in/anurag-shete-053294178,,Stripe,Software Engineer,25 Sep 2026
Some,Stranger,https://www.linkedin.com/in/stranger,,Acme,CEO,24 Sep 2026
"""
LI_MESSAGES = """CONVERSATION ID,CONVERSATION TITLE,FROM,SENDER PROFILE URL,TO,RECIPIENT PROFILE URLS,DATE,SUBJECT,CONTENT,FOLDER
c1,,Me Myself,https://www.linkedin.com/in/me,Tom Becker,https://www.linkedin.com/in/tom-becker-5,2026-09-10 14:00:00 UTC,,Hi Tom,SENT
c1,,Tom Becker,https://www.linkedin.com/in/tom-becker-5,Me Myself,https://www.linkedin.com/in/me,2026-09-12 10:00:00 UTC,,Happy to chat,INBOX
c2,,Cold Recruiter,https://www.linkedin.com/in/cold-recruiter,Me Myself,https://www.linkedin.com/in/me,2026-09-11 10:00:00 UTC,,We have a role,INBOX
c3,,Me Myself,https://www.linkedin.com/in/me,Dana Ortiz,https://www.linkedin.com/in/dana-ortiz-9,2026-08-01 14:00:00 UTC,,Hello Dana,SENT
"""


def test_linkedin_export_import(env):
    db, sync = env
    from jobtracker import importers
    files = {"Invitations.csv": LI_INVITES, "Connections.csv": LI_CONNECTIONS, "messages.csv": LI_MESSAGES}
    plan = importers.plan_linkedin(files, since="2026-08-15")
    names = {p["name"]: p for p in plan["people"]}
    assert set(names) == {"Anurag Shete", "Priya Raman", "Tom Becker"}           # no stranger, no inbound recruiter, Dana too old
    assert plan["skipped_incoming_invites"] == 1
    a = names["Anurag Shete"]
    assert (a["sent_at"], a["responded_at"], a["kind"], a["company"]) == ("2026-09-20", "2026-09-25", "Accepted invite", "Stripe")
    assert names["Priya Raman"]["responded_at"] is None                              # invited, no answer (yet)
    assert (names["Tom Becker"]["kind"], names["Tom Becker"]["responded_at"]) == ("Replied", "2026-09-12")

    with db.session() as c:
        s = importers.summarize(c, plan)
        assert (s["total"], s["new"], s["existing"], s["with_response"]) == (3, 3, 0, 2)
        assert importers.apply_plan(c, plan, "linkedin-import") == {"created": 3, "updated": 0, "responses": 2}
        assert importers.apply_plan(c, plan, "linkedin-import") == {"created": 0, "updated": 3, "responses": 0}   # idempotent
        rows = {r["name"]: r for r in c.execute("SELECT * FROM contacts")}
        assert rows["Anurag Shete"]["stage"] == "Replied" and rows["Priya Raman"]["stage"] == "Reached out"
        assert rows["Tom Becker"]["outreach_sent_at"] == "2026-09-10"


def test_sheet_import(env):
    db, sync = env
    from jobtracker import importers
    csv_text = ("Name,Company,Position,LinkedIn,Date Contacted,Status,Notes\n"
                "Marcus Lee,Datadog,EM,https://linkedin.com/in/marcus-lee-1,03/05/2026,Had a coffee chat,Great convo\n"
                "Aisha Khan,Ramp,Recruiter,,,Replied,\n"
                "Tom Becker,Figma,SWE,,09/01/2026,No response,\n"
                ",,,,,,\n")
    plan = importers.plan_sheet(csv_text)
    assert plan["mapping"]["name"] == "Name" and plan["mapping"]["date"] == "Date Contacted"
    assert [(p["name"], p["stage"]) for p in plan["people"]] == [("Marcus Lee", "Chatted"), ("Aisha Khan", "Replied"), ("Tom Becker", "Reached out")]
    with db.session() as c:
        importers.apply_plan(c, plan, "sheet-import")
        rows = {r["name"]: r for r in c.execute("SELECT * FROM contacts")}
        assert rows["Marcus Lee"]["stage"] == "Chatted" and rows["Marcus Lee"]["ext_key"] == "marcus-lee-1"
        assert rows["Marcus Lee"]["channel"] == "LinkedIn" and rows["Tom Becker"]["outreach_sent_at"] == "2026-09-01"


def test_backfilled_responses_do_not_count_as_new_for_the_digest(env):
    db, sync = env
    from datetime import date, timedelta
    from jobtracker import outreach
    with db.session() as c:
        old, recent = (date.today() - timedelta(days=40)).isoformat(), (date.today() - timedelta(days=1)).isoformat()
        a, _ = outreach.record_response(c, name="Old Timer", slug="old-timer-1", when=old, kind="Accepted invite", create_missing=True)
        b, _ = outreach.record_response(c, name="Fresh Face", slug="fresh-face-2", when=recent, kind="Accepted invite", create_missing=True)
        logged = {r["id"]: r["response_logged_at"] for r in c.execute("SELECT id, response_logged_at FROM contacts")}
    assert logged[a] is None and logged[b] is not None


def test_profile_card_parsing_and_name_upgrade(env):
    db, sync = env
    from jobtracker import outreach
    card = ("See Bharath Prakash’s connections, experience, and more\n Bharath has accepted your invitation \n Bharath Prakash \n"
            " Actively seeking Co-op for Fall ’26 | USC ’26 | AI Engineering \n Message \n Suggestions from Bharath’s network \n Athish Raj Mohan \n")
    r = outreach.parse_profile_card(card, "Bharath")
    assert r["full_name"] == "Bharath Prakash" and r["headline"].startswith("Actively seeking Co-op") and r["company"] == ""
    r2 = outreach.parse_profile_card("See Anu Rao’s connections, experience, and more\n Anu Rao \n MS CS @ USC | Software Engineer \n Message", "Anu")
    assert (r2["headline"], r2["company"]) == ("MS CS @ USC | Software Engineer", "")      # a school is not an employer: no guessing
    assert outreach.parse_profile_card("See Someone Else’s connections, experience, and more", "Bharath") == {}

    with db.session() as c:     # a first-name-only contact (created before the card was parsed) gets repaired in place
        cid, _ = outreach.record_response(c, name="Bharath", slug="bharathprakashusc", when="2026-09-17", kind="Accepted invite", create_missing=True)
        assert c.execute("SELECT name FROM contacts WHERE id=?", (cid,)).fetchone()[0] == "Bharath"
        outreach.record_response(c, name="Bharath Prakash", slug="bharathprakashusc", when="2026-09-17", kind="Accepted invite",
                                 title="Actively seeking Co-op for Fall ’26")
        row = c.execute("SELECT name, title FROM contacts WHERE id=?", (cid,)).fetchone()
        assert (row["name"], row["title"]) == ("Bharath Prakash", "Actively seeking Co-op for Fall ’26")
        assert c.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] == 1


def test_first_name_only_contact_upgrades_instead_of_duplicating(env):
    db, sync = env
    from jobtracker import outreach
    with db.session() as c:
        a, _ = outreach.record_response(c, name="Ishaan", when="2026-09-14", kind="Accepted invite", create_missing=True)   # no profile link
        b, how = outreach.record_response(c, name="Ishaan Singh", when="2026-09-14", kind="Accepted invite", create_missing=True)
        assert (b, how) == (a, "unchanged") and c.execute("SELECT name FROM contacts").fetchone()[0] == "Ishaan Singh"
        # two bare first names make it ambiguous: a new full-name contact is created rather than guessing
        outreach.record_response(c, name="Sahil", when="2026-09-09", kind="Accepted invite", create_missing=True)
        c.execute("INSERT INTO contacts (name, ext_key) VALUES ('Sahil', '')")
        _, how = outreach.record_response(c, name="Sahil Patil", when="2026-09-09", kind="Accepted invite", create_missing=True)
        assert how == "created"


def test_sheet_import_networking_crm_layout(env):
    db, sync = env
    from jobtracker import importers
    csv_text = (
        "Name,Company,Role/Title,Role type,LinkedIn URL,Priority,Connection hook,Hook type,Status,Date first contacted,Date last touch,"
        "Message sent (summary),Notes / what they said,Next step,Referral given?\n"
        "Ann Lee,Pinterest,Data Scientist,IC Engineer,https://www.linkedin.com/in/ann-lee/,,,Cold/no hook,Not contacted,,,,,,\n"
        "Bo Chan,Spotify,Data Engineer,IC Engineer,https://www.linkedin.com/in/bo-chan-9/,High,Same project,Project relevance,Messaged,09/20/2026,09/27/2026,Asked about team,,Follow up Friday,\n"
        "Cy Dunn,Apple Music,MLE,Senior/Lead Engineer,,,,,Replied,,,,Said to apply online,,Yes\n")
    plan = importers.plan_sheet(csv_text)
    m = plan["mapping"]
    assert (m["title"], m["link"], m["notes"], m["date"], m["last_touch"], m["status"]) == (
        "Role/Title", "LinkedIn URL", "Notes / what they said", "Date first contacted", "Date last touch", "Status")
    assert "Role type" in plan["extra_columns"] and "Role/Title" not in plan["extra_columns"]
    by = {p["name"]: p for p in plan["people"]}
    assert by["Ann Lee"]["stage"] == "To reach out" and by["Bo Chan"]["stage"] == "Reached out" and by["Cy Dunn"]["stage"] == "Referred"
    assert by["Bo Chan"]["title"] == "Data Engineer" and by["Bo Chan"]["last_touch"] == "2026-09-27"
    assert "Priority: High" in by["Bo Chan"]["notes"] and "Next step: Follow up Friday" in by["Bo Chan"]["notes"] and "Hook type: Project relevance" in by["Bo Chan"]["notes"]
    assert by["Cy Dunn"]["notes"].startswith("Said to apply online")

    with db.session() as c:
        importers.apply_plan(c, plan, "sheet-import")
        rows = {r["name"]: r for r in c.execute("SELECT * FROM contacts")}
    assert rows["Ann Lee"]["stage"] == "To reach out" and rows["Ann Lee"]["needs_triage"] == 0 and rows["Ann Lee"]["ext_key"] == "ann-lee"
    assert (rows["Bo Chan"]["outreach_sent_at"], rows["Bo Chan"]["last_contacted_at"], rows["Bo Chan"]["stage"]) == ("2026-09-20", "2026-09-27", "Reached out")
    assert rows["Cy Dunn"]["stage"] == "Referred"


def test_sheet_import_marks_existing_linkedin_people_as_sorted(env):
    db, sync = env
    from jobtracker import importers, outreach
    with db.session() as c:
        outreach.record_response(c, name="Nimita Singh", slug="nimita-singh-1", when="2026-08-14", kind="Replied",
                                 create_missing=True, source="linkedin-email")
        assert c.execute("SELECT needs_triage FROM contacts").fetchone()[0] == 1
        plan = importers.plan_sheet("Name,Company,LinkedIn URL,Status\nNimita Singh,Apple Music,https://www.linkedin.com/in/nimita-singh-1,Replied\n")
        assert importers.summarize(c, plan)["existing"] == 1
        importers.apply_plan(c, plan, "sheet-import")
        row = c.execute("SELECT needs_triage, company, stage FROM contacts").fetchone()
        assert (row["needs_triage"], row["company"], row["stage"]) == (0, "Apple Music", "Replied")
        assert c.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] == 1


def test_search_query_excludes_your_own_mail():
    from datetime import date
    from jobtracker import gmail
    assert "-from:me" in gmail.build_query(date(2026, 10, 1))
