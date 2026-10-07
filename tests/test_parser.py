from jobtracker.parser import parse_email, rule_status, find_req_id


def P(subject, sender, body=""):
    return parse_email(subject, sender, body, use_llm=False)


def test_greenhouse_applied():
    p = P("Thank you for applying to Applied Intuition!", "Applied Intuition <no-reply@us.greenhouse-mail.io>",
          "Thanks for applying to Applied Intuition. Our team will review.")
    assert p.is_job and p.status == "Applied" and p.company == "Applied Intuition"


def test_workday_rejection():
    p = P("Update for REQ343392 2026 Summer AI Engineering Internship", "MyWorkday T-Mobile <tmobile@myworkday.com>",
          "Thank you for your interest. We have decided to pursue other candidates and will not be moving forward.")
    assert p.status == "Rejected" and p.company == "T-Mobile" and p.req_id == "343392"


def test_confirmation_with_unfortunately_is_applied():
    s = rule_status("Thanks for applying", "Thanks for applying! Unfortunately we cannot reply to everyone.")
    assert s == "Applied"


def test_job_board_blocked():
    assert not P("Jobs you might like", "LinkedIn <jobs-listings@linkedin.com>").is_job


def test_linkedin_easy_apply_allowed():
    p = P("Your application was sent to Acme Corp", "LinkedIn <jobs-noreply@linkedin.com>", "Your application was sent")
    assert p.is_job and p.company == "Acme Corp" and p.status == "Applied"


def test_hold_email_is_not_rejection():
    assert rule_status("Update Regarding Your Amazon Application", "You are still under consideration. We await to share next steps.") is None


def test_oa():
    assert rule_status("Invite to Online Assessment | Data Science Intern", "") == "OA"


def test_req_id():
    assert find_req_id("Req ID 210927") == "210927"
    assert find_req_id("Job ID: 3116030") == "3116030"


def test_university_and_company_cleanup():
    p = P("Software Engineer-26006148 at University of Pittsburgh", "University of Pittsburgh <noreply@hr.pitt.edu>", "Thanks for applying")
    assert p.company == "University of Pittsburgh"


def test_icims_tail_stripped_and_platform_not_company():
    p = P("Your job application is incomplete!", '"Cotiviti @ icims" <cotiviti+autoreply@talent.icims.com>', "application received")
    assert p.company == "Cotiviti"
    p2 = P("Your HackerRank Maven Securities Test", "recruitment@mavensecurities.com <support@hackerrank.com>", "online assessment")
    assert "hackerrank" not in p2.company.lower() and "@" not in p2.company


def test_role_cleanup():
    from jobtracker.parser import clean_role
    assert clean_role("applying to Software Engineer,2132", "Amdocs") == "Software Engineer"
    assert clean_role("Roblox Job", "Roblox") == ""
    assert clean_role("Stripe", "Stripe") == ""


def test_roblox_style_rejection_with_thank_you_for_applying():
    body = ("Application Update. Hi, Thank you for applying to our [2027] Software Engineer, Early Career role and taking the "
            "time to complete our assessments. After careful review, we've made the difficult decision to not move forward with your candidacy.")
    p = P("[Update] Your Roblox Job Application", "Roblox <no-reply@roblox.com>", body)
    assert p.status == "Rejected"


def test_applied_with_decision_words_is_not_guessed_applied_without_llm():
    p = P("Thanks for applying", "Foo <no-reply@foo.com>", "Thank you for applying. We will make a decision about your candidacy soon.")
    assert p.status is None


def test_posting_feeds_and_community_orgs_ignored(monkeypatch):
    assert not P("New roles: She & HER - ML Intern", "Wellfound <jobs@wellfound.com>", "apply now").is_job
    assert not P("Opportunity: ML internship", "Rewriting The Code <hello@rewritingthecode.org>", "apply to join").is_job
    from jobtracker import config
    monkeypatch.setattr(config, "EXTRA_BLOCKED_SENDERS", ("somecommunity.org",))
    assert not P("Thanks for applying to our program", "Team <hi@somecommunity.org>", "application received").is_job


def test_blockable_pattern_never_blocks_shared_hosts():
    from jobtracker.parser import blockable_pattern
    assert blockable_pattern("Acme <no-reply@us.greenhouse-mail.io>") is None
    assert blockable_pattern("Me <someone@gmail.com>") is None
    assert blockable_pattern("Support <support@hackerrankforwork.com>") is None
    assert blockable_pattern("Wellfound <jobs@mail.example-community.org>") == "mail.example-community.org"
    assert parse_email("Thanks for applying", "X <hi@mail.example-community.org>", "application received", use_llm=False,
                       extra_blocked=("example-community.org",)).is_job is False


def test_gmail_query_is_padded_and_covers_submission_phrasing():
    from datetime import date
    from jobtracker import gmail
    q = gmail.build_query(date(2026, 10, 6), date(2026, 10, 7))
    assert "after:2026/10/05" in q and "before:2026/10/09" in q
    assert '"online submission"' in q and '"screening"' in q


def test_no_llm_call_when_email_has_no_job_signal(monkeypatch):
    from jobtracker import parser
    calls = []
    monkeypatch.setattr(parser, "llm_available", lambda: True)
    monkeypatch.setattr(parser, "llm_parse", lambda *a: calls.append(a) or {"is_job_related": False})
    p = parser.parse_email("Account update for your HDFC Bank A/c", "HDFC <alerts@hdfcbank.net>", "Your balance is 10,000.")
    assert not p.is_job and calls == []
    parser.parse_email("Re: quick chat", "Someone <a@b.com>", "Let's talk about the position and your resume.")
    assert len(calls) == 1


def test_online_submission_receipt():
    p = P("Thank you for your online submission", "Hewlett Packard Enterprise <hpe@myworkday.com>", "We have received your application.")
    assert p.status == "Applied" and p.is_job


def test_receipt_subject_detection():
    from jobtracker.parser import is_receipt_subject
    assert is_receipt_subject("Thanks for applying to SpaceXAI!")
    assert is_receipt_subject("We received your Stripe Application")
    assert not is_receipt_subject("Roblox Application: Reminder to Verify Your Email Address")
    assert not is_receipt_subject("[Update] Your Roblox Job Application")
