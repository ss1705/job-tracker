import importlib

import pytest

from jobtracker import config, drafting, resume

TestClient = pytest.importorskip("fastapi.testclient").TestClient

RESUME = "Alex Rivera\nEducation: University of Southern California, M.S. Computer Science 2027\n" + "Built a two-tower retrieval model in PyTorch. " * 8


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "d.db"))
    monkeypatch.setenv("AUTO_SYNC_MINUTES", "0")
    monkeypatch.setenv("DIGEST_ENABLED", "0")
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setenv("LINKEDIN_NOTE_MAX", "200")          # tests must not depend on the developer's own .env
    from jobtracker import config, db, followups, sync, scheduler, contacts, drafts_api, resume as r, drafting as d, api
    for m in (config, db, followups, sync, scheduler, contacts, r, d, drafts_api, api):
        importlib.reload(m)
    with TestClient(api.app) as c:
        yield c, d, r


GOOD_NOTE = ("Hi Jordan, fellow Trojan here. Your two-tower candidate generation work at Spotify caught my eye; "
             "I built a two-tower retriever with PyTorch and FAISS. Could I ask you two questions?")
assert 165 <= len(GOOD_NOTE) <= 195


def fake_llm(note=GOOD_NOTE):
    calls = []

    def chat_json(prompt, **kw):
        calls.append(prompt)
        return {"recipient": {"name": "Jordan Lee", "role": "Senior ML Engineer", "company": "Spotify", "education": ["USC"]},
                "hooks": [{"claim": "shared USC", "source": "profile", "quote": "University of Southern California"}],
                "note": note, "message": "Hi Jordan,\n\nI'm Alex, a fellow Trojan. Would love your advice.\n\nBest,\nAlex", "warnings": []}
    return chat_json, calls


def test_detect_school_and_names(monkeypatch):
    monkeypatch.setattr(config, "SCHOOL_NAME", "USC")
    monkeypatch.setattr(config, "SCHOOL_TERMS", ["University of Southern California", "Trojan"])
    monkeypatch.setattr(config, "SCHOOL_NOT", ["South Carolina"])
    assert drafting.detect_school("Education: University of Southern California")["status"] == "yes"
    assert drafting.detect_school("MS CS @ USC")["status"] == "maybe"
    assert drafting.detect_school("University of South Carolina")["status"] == "no"
    assert drafting.detect_school("MIT")["status"] == "no"
    monkeypatch.setattr(config, "SCHOOL_NAME", "")
    assert drafting.detect_school("University of Southern California")["status"] == "no"      # feature off without a school
    assert drafting.name_from_slug("jane-doe-ph-d-12345678") == "Jane Doe"
    assert drafting.clean_profile_url("https://www.linkedin.com/in/jane-doe-ph-d-12345678?utm_source=share_via&utm_medium=member_ios") == (
        "https://www.linkedin.com/in/jane-doe-ph-d-12345678", "jane-doe-ph-d-12345678")
    assert drafting._tidy("two‑tower model") == "two-tower model"


def test_unsupported_claims_become_warnings(client, monkeypatch):
    c, d, r = client
    r.save("cv.txt", RESUME.encode())
    chat, calls = fake_llm()

    def chat_with_verifier(prompt, **kw):
        if "strict fact-checker" in prompt:
            return {"unsupported": [{"claim": "now lead retrieval", "issue": "profile says builds, not leads"}]}
        return chat(prompt, **kw)
    monkeypatch.setattr(d.llm, "chat_json", chat_with_verifier)
    out = d.draft(name="Jordan Lee", profile_text="Jordan Lee, builds retrieval systems at Spotify. " * 5)
    assert any(w.startswith("Check: “now lead retrieval”") for w in out["warnings"])

    monkeypatch.setattr(d.llm, "chat_json", lambda prompt, **kw: (_ for _ in ()).throw(d.llm.LLMExhausted("quota")) if "strict fact-checker" in prompt else chat(prompt, **kw))
    assert d.draft(name="Jordan Lee", profile_text="x " * 100)["note"]              # a failed check never blocks the draft


def test_recruiter_draft_is_tied_to_the_application_and_followups_need_a_sent_message(client, monkeypatch):
    c, d, r = client
    from jobtracker import db, recruiter
    r.save("cv.txt", RESUME.encode())
    with db.session() as conn:
        app_id = conn.execute("INSERT INTO applications (company, company_key, role, status, applied_at, source) VALUES "
                              "('Acme','acme','ML Engineer','Saved','2026-10-20','Manual')").lastrowid
    prompts = []

    def chat_json(prompt, **kw):
        prompts.append(prompt)
        if "strict fact-checker" in prompt:
            return {"unsupported": []}
        if prompt.startswith("Pick up to 3 lines"):
            return {"points": [{"quote": "Built a two-tower retrieval model in PyTorch.", "why": "retrieval"},
                               {"quote": "Led a team of twelve engineers.", "why": "invented, not in the resume"}]}
        if "polite follow-up" in prompt:
            return {"message": "Hi Sam, one more detail that may help: I built a retrieval model. Is there a good person to ask?", "email_subject": "Question",
                    "email_body": "Hi Sam,\n\nOne more detail. Is there a better person to ask?\n\nAlex", "warnings": []}
        return {"recipient": {"name": "Sam Park", "role": "Recruiter", "company": "Acme"}, "hooks": [], "note": GOOD_NOTE,
                "email_subject": "ML Engineer at Acme", "email_body": "Hello Sam,\n\nI plan to apply for the ML Engineer role.\n\nAlex", "warnings": []}
    monkeypatch.setattr(d.llm, "chat_json", chat_json)

    res = c.post("/api/outreach/recruiter", json={"application_id": app_id, "name": "Sam Park", "job_text": "We need retrieval and ranking experience."}).json()
    dr = res["draft"]
    assert dr["kind"] == "recruiter" and dr["email"]["subject"] == "ML Engineer at Acme"
    assert [p["quote"] for p in dr["points"]] == ["Built a two-tower retrieval model in PyTorch."]      # the invented line is dropped
    assert "NOT applied yet" in next(p for p in prompts if "write a short, honest message to a recruiter" in p)   # status 'Saved': never claim to have applied
    assert res["contact"]["application_id"] == app_id
    saved = c.get(f"/api/outreach/application/{app_id}").json()
    assert saved["job_text"].startswith("We need retrieval") and saved["recruiters"][0]["name"] == "Sam Park"

    cid = res["contact"]["id"]
    assert c.post(f"/api/outreach/followup/{cid}", json={}).status_code == 422                            # nothing logged as sent yet
    c.post(f"/api/contacts/{cid}/mark-sent", json={"which": "email", "text": "Subject: ML Engineer at Acme\n\nHello Sam"})
    fu = c.post(f"/api/outreach/followup/{cid}", json={"tone": "warm"}).json()["followup"]
    assert fu["kind"] == "followup" and fu["days_since"] == 0
    assert "Hello Sam" in next(p for p in prompts if "polite follow-up" in p)                              # it sees what you actually sent
    assert c.get(f"/api/outreach/contact/{cid}").json()["draft"]["followup"]["message"] == fu["message"]
    assert c.post("/api/outreach/recruiter", json={"application_id": app_id, "tone": "sarcastic"}).status_code == 422
