import importlib

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
