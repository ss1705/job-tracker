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


def test_action_needed_emails_are_recognised_but_hedged_receipts_are_not():
    imc = P("Acme Application: Additional Info Needed", "Campus <campus@acme.com>",
            "You are receiving this request for additional information because you answered Yes to the sponsorship question. "
            "Completion of this form is needed in order to process your application, so please complete it at your earliest convenience.")
    assert imc.status == "Action" and imc.is_job
    verify = P("Zeta Application: Reminder to Verify Your Email Address", "no-reply@email.zeta.com",
               "We haven't heard from you. Please verify your email address to proceed to the next stage of your recruitment process.")
    assert verify.status == "Action"
    incomplete = P("Your job application is incomplete!", '"Beta @ icims" <beta+autoreply@talent.icims.com>', "Finish your application to be considered.")
    assert incomplete.status == "Action" and incomplete.company == "Beta"

    # ordinary receipts that merely mention possible future requests must stay receipts
    receipt = P("Thank you for applying to Acme!", "Acme <no-reply@us.greenhouse-mail.io>",
                "Thanks for applying. If we need additional information, we will contact you. Our team will review your application.")
    assert receipt.status == "Applied"
    assert P("Your HackerRank assessment", "Acme <no-reply@acme.com>", "Please complete the online assessment within 7 days.").status == "OA"
    assert P("Rejection", "Acme <no-reply@acme.com>", "Unfortunately we will not be moving forward. Please verify your email.").status == "Rejected"   # outcomes outrank requests


def test_action_summary_picks_the_request_not_the_boilerplate():
    from jobtracker.parser import action_summary
    s = action_summary("Additional Info Needed", "Hello Alex. Thanks for your interest. Completion of this form is needed, so please complete the form soon. Regards.")
    assert "please complete the form" in s
    assert action_summary("Additional Info Needed", "Hello. Regards.") == "Additional Info Needed"


def test_action_needs_job_context_and_eligibility_rejections_are_rejections():
    # generic "verify / action required" mail from non-recruiting senders is not an application to-do
    assert P("[Action Required] Transition your scripts to the new tool", "Google Cloud <noreply@google.com>", "Please update your workflows before March.").status is None
    assert not P("Verify your email address", "Streamlit <no-reply@streamlit.io>", "Please verify your email address to continue.").is_job
    assert not P("Confirmation on e-Verification of Income Tax Return", "tax@gov.in", "Your return was e-verified. Please confirm your email.").is_job
    # but the same words from a recruiting system or in an application context do count
    assert P("Verify your candidate account", "Barclays <barclays@barclays.com>", "Please verify your email to continue.").status == "Action"
    assert P("Please verify your email", "Acme <no-reply@us.greenhouse-mail.io>", "Verify your email address to finish.").status == "Action"
    body = ("Thank you for applying. During the application process you indicated you are not authorized to work in the U.S. "
            "At this time we are only able to consider candidates who do not require sponsorship. If this selection was made in error, please reply.")
    assert P("Application Follow Up", "no-reply@getgarner.com", body).status == "Rejected"
