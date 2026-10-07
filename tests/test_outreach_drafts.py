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
    from jobtracker import config, db, followups, sync, scheduler, contacts, drafts_api, resume as r, drafting as d, api
    for m in (config, db, followups, sync, scheduler, contacts, r, d, drafts_api, api):
        importlib.reload(m)
    with TestClient(api.app) as c:
        yield c, d, r


def fake_llm(note="Hi Jordan, fellow Trojan here. I build two-tower retrieval models; could I ask you a quick question about your Spotify work?"):
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
    assert "fact-checker" in calls[1]                                              # every draft is independently verified
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


def test_overlong_note_is_retried_then_flagged(client, monkeypatch):
    c, d, r = client
    r.save("cv.txt", RESUME.encode())
    long_note = "x" * 230
    chat, calls = fake_llm(note=long_note)
    monkeypatch.setattr(d.llm, "chat_json", chat)
    out = d.draft(name="Jordan Lee", profile_text="Jordan Lee, Spotify. " * 10)
    assert len(calls) == 3 and "must be at most" in calls[1] and "fact-checker" in calls[2]   # shorten once, then verify
    assert any("200" in w and "Trim" in w for w in out["warnings"])               # still too long: tell the user


def test_unsupported_claims_become_warnings(client, monkeypatch):
    c, d, r = client
    r.save("cv.txt", RESUME.encode())
    chat, calls = fake_llm()

    def chat_with_verifier(prompt, **kw):
        if "fact-checker" in prompt:
            return {"unsupported": [{"claim": "now lead retrieval", "issue": "profile says builds, not leads"}]}
        return chat(prompt, **kw)
    monkeypatch.setattr(d.llm, "chat_json", chat_with_verifier)
    out = d.draft(name="Jordan Lee", profile_text="Jordan Lee, builds retrieval systems at Spotify. " * 5)
    assert any(w.startswith("Check: “now lead retrieval”") for w in out["warnings"])

    monkeypatch.setattr(d.llm, "chat_json", lambda prompt, **kw: (_ for _ in ()).throw(d.llm.LLMExhausted("quota")) if "fact-checker" in prompt else chat(prompt, **kw))
    assert d.draft(name="Jordan Lee", profile_text="x " * 100)["note"]              # a failed check never blocks the draft
