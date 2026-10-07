import base64
import importlib
import io
import zipfile

import pytest

from jobtracker import drafting, resume

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


def test_detect_usc_and_names():
    assert drafting.detect_usc("Education: University of Southern California")["status"] == "yes"
    assert drafting.detect_usc("MS CS @ USC")["status"] == "maybe"
    assert drafting.detect_usc("University of South Carolina")["status"] == "no"
    assert drafting.detect_usc("MIT")["status"] == "no"
    assert drafting.name_from_slug("timothy-greer-ph-d-28630671") == "Timothy Greer"
    assert drafting.clean_profile_url("https://www.linkedin.com/in/timothy-greer-ph-d-28630671?utm_source=share_via&utm_medium=member_ios") == (
        "https://www.linkedin.com/in/timothy-greer-ph-d-28630671", "timothy-greer-ph-d-28630671")
    assert drafting._tidy("two‑tower model") == "two-tower model"


def test_resume_extraction(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "x.db"))
    from jobtracker import config
    importlib.reload(config); importlib.reload(resume)
    assert not resume.status()["present"]
    resume.save("cv.txt", RESUME.encode())
    assert resume.status()["present"] and resume.first_name() == "Alex"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", "<w:document><w:p><w:t>" + "Jane Q Public " * 10 + "&amp; more</w:t></w:p></w:document>")
    assert "Jane Q Public" in resume.extract_text("cv.docx", buf.getvalue())
    with pytest.raises(resume.ResumeError):
        resume.extract_text("cv.exe", b"x")
    with pytest.raises(resume.ResumeError):
        resume.extract_text("cv.txt", b"too short")


def test_draft_flow_creates_contact_and_logs_send(client, monkeypatch):
    c, d, r = client
    chat, calls = fake_llm()
    monkeypatch.setattr(d.llm, "chat_json", chat)

    # needs a resume first
    assert c.post("/api/outreach/draft", json={"url": "https://www.linkedin.com/in/jordan-lee-123"}).status_code == 422
    up = c.post("/api/resume", json={"filename": "cv.txt", "data": base64.b64encode(RESUME.encode()).decode()})
    assert up.status_code == 200 and up.json()["present"]

    profile = "Jordan Lee\nSenior ML Engineer at Spotify\nEducation: University of Southern California, M.S. Computer Science"
    res = c.post("/api/outreach/draft", json={"url": "https://www.linkedin.com/in/jordan-lee-123?utm_source=share", "profile_text": profile})
    assert res.status_code == 200
    body = res.json()
    assert body["draft"]["usc"]["used"] == "yes" and len(body["draft"]["note"]) <= 200
    assert "authoritative" in calls[0] and "yes, the recipient attended USC" in calls[0]
    assert "strict fact-checker" in calls[1]                                      # every draft is independently verified
    cid = body["contact"]["id"]
    assert (body["contact"]["name"], body["contact"]["company"], body["contact"]["stage"]) == ("Jordan Lee", "Spotify", "To reach out")
    assert body["contact"]["link"] == "https://www.linkedin.com/in/jordan-lee-123"

    # same person again reuses the contact; saved draft can be reopened
    again = c.post("/api/outreach/draft", json={"url": "https://www.linkedin.com/in/jordan-lee-123", "usc_override": "no"})
    assert again.json()["contact"]["id"] == cid and "no (confirmed by the sender)" in calls[-2]
    assert c.get(f"/api/outreach/contact/{cid}").json()["draft"]["note"]
    assert [p["name"] for p in c.get("/api/contacts").json()] == ["Jordan Lee"]

    # "I sent it" logs the outreach
    sent = c.post(f"/api/contacts/{cid}/mark-sent", json={"which": "note", "text": "Hi Jordan..."})
    assert sent.json()["stage"] == "Reached out"
    p = c.get("/api/contacts").json()[0]
    assert p["stage"] == "Reached out" and p["channel"] == "LinkedIn" and p["outreach_sent_at"] and "Hi Jordan..." in p["notes"]
    assert c.post(f"/api/contacts/{cid}/mark-sent", json={"which": "oops"}).status_code == 422


def test_overlong_note_is_shortened_by_a_targeted_rewrite(client, monkeypatch):
    c, d, r = client
    r.save("cv.txt", RESUME.encode())
    chat, calls = fake_llm(note="x" * 230)

    shortened = []

    def with_shortener(prompt, **kw):
        if "Rewrite the LinkedIn" in prompt:
            shortened.append(prompt)
            return {"text": GOOD_NOTE}
        return chat(prompt, **kw)
    monkeypatch.setattr(d.llm, "chat_json", with_shortener)
    out = d.draft(name="Jordan Lee", profile_text="Jordan Lee, Spotify. " * 10)
    assert out["note"] == GOOD_NOTE and not any("Trim" in w for w in out["warnings"])
    assert len(shortened) == 1 and "AT MOST 195 characters" in shortened[0]           # one targeted rewrite, asked for under the limit


def test_overlong_note_that_cannot_be_fitted_is_flagged(client, monkeypatch):
    c, d, r = client
    r.save("cv.txt", RESUME.encode())
    chat, calls = fake_llm(note="x" * 230)
    monkeypatch.setattr(d.llm, "chat_json", chat)                                     # the "shortener" gets no usable answer
    out = d.draft(name="Jordan Lee", profile_text="Jordan Lee, Spotify. " * 10)
    assert any("200" in w and "Trim" in w for w in out["warnings"])


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


def test_short_note_is_retried_and_limit_is_configurable(client, monkeypatch):
    c, d, r = client
    r.save("cv.txt", RESUME.encode())
    short = "Hi Jordan, quick chat about Spotify?"
    chat, calls = fake_llm(note=short)
    monkeypatch.setattr(d.llm, "chat_json", chat)
    out = d.draft(name="Jordan Lee", profile_text="Jordan Lee, Spotify. " * 10)
    assert len(calls) == 3 and f"only {len(short)} characters" in calls[1]                    # too short: asked to add the specific detail
    assert out["limits"]["note_max"] == 200 and not any("Trim" in w for w in out["warnings"])   # short is kept if the retry doesn't help

    monkeypatch.setattr(d.config, "LINKEDIN_NOTE_MAX", 300)                          # Premium
    chat2, calls2 = fake_llm()
    monkeypatch.setattr(d.llm, "chat_json", chat2)
    out2 = d.draft(name="Jordan Lee", profile_text="Jordan Lee, Spotify. " * 10)
    assert out2["limits"]["note_max"] == 300 and "between 246 and 295 characters" in calls2[0]


def test_flagged_claims_get_one_repair_pass_and_only_leftovers_are_shown(client, monkeypatch):
    c, d, r = client
    r.save("cv.txt", RESUME.encode())
    chat, calls = fake_llm()
    REPAIRED_NOTE = ("Hi Jordan, fellow Trojan here. Your two-tower candidate generation work at Spotify caught my eye; "
                     "I built a two-tower retriever with PyTorch and FAISS. Could I ask how you evaluate yours?")
    assert len(REPAIRED_NOTE) <= 200
    state = {"verifies": 0}

    def llm_stub(prompt, **kw):
        if "Fix this LinkedIn outreach draft" in prompt:                              # (its text mentions a fact-checker too: test it first)
            assert "I'm exploring RAG" in prompt                                      # the repair is told exactly what to fix
            return {"note": REPAIRED_NOTE, "message": "Hi Jordan,\n\nRepaired message.\n\nBest,\nAlex"}
        if "strict fact-checker" in prompt:
            state["verifies"] += 1
            return {"unsupported": [{"claim": "I'm exploring RAG", "issue": "resume does not say this"}]} if state["verifies"] == 1 else {"unsupported": []}
        return chat(prompt, **kw)
    monkeypatch.setattr(d.llm, "chat_json", llm_stub)
    out = d.draft(name="Jordan Lee", profile_text="Jordan Lee, Spotify. " * 10)
    assert out["note"] == REPAIRED_NOTE and out["message"].startswith("Hi Jordan,\n\nRepaired")
    assert state["verifies"] == 2 and not any(w.startswith("Check:") for w in out["warnings"])   # fixed, so nothing to warn about


def test_warns_when_a_backup_model_wrote_the_draft(client, monkeypatch):
    c, d, r = client
    r.save("cv.txt", RESUME.encode())
    chat, calls = fake_llm()

    def stub(prompt, **kw):
        d.llm.last_model = "openai/gpt-oss-20b"                                       # what llm.chat_json records on a fallback
        return chat(prompt, **kw)
    monkeypatch.setattr(d.llm, "chat_json", stub)
    out = d.draft(name="Jordan Lee", profile_text="Jordan Lee, Spotify. " * 10)
    assert out["warnings"][0].startswith("Drafted with a smaller backup model (openai/gpt-oss-20b)")
