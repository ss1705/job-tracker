from jobtracker.parser import parse_email, rule_status


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


def test_hold_email_is_not_rejection():
    assert rule_status("Update Regarding Your Amazon Application", "You are still under consideration. We await to share next steps.") is None


def test_oa():
    assert rule_status("Invite to Online Assessment | Data Science Intern", "") == "OA"


def test_university_and_company_cleanup():
    p = P("Software Engineer-26006148 at University of Pittsburgh", "University of Pittsburgh <noreply@hr.pitt.edu>", "Thanks for applying")
    assert p.company == "University of Pittsburgh"


def test_role_cleanup():
    from jobtracker.parser import clean_role
    assert clean_role("applying to Software Engineer,2132", "Amdocs") == "Software Engineer"
    assert clean_role("Roblox Job", "Roblox") == ""
    assert clean_role("Stripe", "Stripe") == ""


def test_applied_with_decision_words_is_not_guessed_applied_without_llm():
    p = P("Thanks for applying", "Foo <no-reply@foo.com>", "Thank you for applying. We will make a decision about your candidacy soon.")
    assert p.status is None


def test_gmail_query_is_padded_and_covers_submission_phrasing():
    from datetime import date
    from jobtracker import gmail
    q = gmail.build_query(date(2026, 10, 6), date(2026, 10, 7))
    assert "after:2026/10/05" in q and "before:2026/10/09" in q
    assert '"online submission"' in q and '"screening"' in q
    assert "-from:me" in q
    q = gmail.build_query(date(2026, 10, 1))
    assert "from:(" in q and "recruiting" in q and "myworkday.com" in q and "candidate.fyi" in q
    assert '"job application"' in q and '"candidate"' in q
    assert q.count("-category:promotions") == 1 and "OR from:(" in q            # the tab exclusion applies to keywords only


def test_no_llm_call_when_email_has_no_job_signal(monkeypatch):
    from jobtracker import parser
    calls = []
    monkeypatch.setattr(parser, "llm_available", lambda: True)
    monkeypatch.setattr(parser, "llm_parse", lambda *a: calls.append(a) or {"is_job_related": False})
    p = parser.parse_email("Account update for your HDFC Bank A/c", "HDFC <alerts@hdfcbank.net>", "Your balance is 10,000.")
    assert not p.is_job and calls == []
    parser.parse_email("Re: quick chat", "Someone <a@b.com>", "Let's talk about the position and your resume.")
    assert len(calls) == 1


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


def test_conditional_language_in_receipts_is_not_an_outcome():
    # real shapes from receipts that were misread as OA / Interview / Rejected. Without the LLM the rules may decline to
    # label a receipt that has decision-sounding words (they leave it to the model), but they must never call it an outcome.
    wrong = {"OA", "Interview", "Rejected", "Offer"}
    receipts = [
        ("Acme <no-reply@us.greenhouse-mail.io>", "Thanks for applying. The first step for candidates who move forward is a HackerRank assessment. "
         "You will receive an email from HackerRank with a deadline if your application is selected for the next stage."),
        ("Beta <no-reply@ashbyhq.com>", "Thanks for applying. If your skills are a strong match, a member of our team will reach out to schedule an interview."),
        ("Gamma <no-reply@us.greenhouse-mail.io>", "Thanks for applying. Due to high volume we may not be able to get back if you are not selected."),
        ("Visa <visa@myworkday.com>", "Thank you for applying. Your application will be taken into careful consideration by our team."),
    ]
    for sender, body in receipts:
        got = P("Thank you for applying", sender, body).status
        assert got not in wrong, (sender, got)
    assert P("Thank you for applying to Beta", "Beta <no-reply@ashbyhq.com>", receipts[1][1]).status == "Applied"


def test_real_outcomes_still_count_when_a_conditional_follows_in_another_clause():
    assert P("Update", "Acme <no-reply@acme.com>", "We have decided to move forward with other candidates, but if a role opens we will contact you.").status == "Rejected"
    assert P("Update", "Acme <no-reply@acme.com>", "We will not be moving forward with your application. If you have questions, reply here.").status == "Rejected"
    assert P("Your assessment", "Acme <no-reply@acme.com>", "Please complete the online assessment within 7 days; if you have trouble, contact us.").status == "OA"
    assert P("Interview invitation", "Acme <no-reply@acme.com>", "We would like to schedule an interview with you next week.").status == "Interview"
    assert P("After careful consideration", "Acme <no-reply@acme.com>", "After careful consideration, we have decided to pursue other applicants.").status == "Rejected"
    assert P("Your offer", "Acme <no-reply@acme.com>", "We are pleased to offer you the position.").status == "Offer"
