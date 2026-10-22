import importlib
import json

import pytest

TestClient = pytest.importorskip("fastapi.testclient").TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "e.db"))
    monkeypatch.setenv("AUTO_SYNC_MINUTES", "0")
    monkeypatch.setenv("DIGEST_ENABLED", "0")
    monkeypatch.setenv("GROQ_API_KEY", "")                      # rules only: deterministic, no network
    monkeypatch.setenv("ENABLE_EVAL", "1")
    from jobtracker import config, db, followups, sync, scheduler, contacts, drafts_api, evalmetrics, evalset, eval_api, api
    for m in (config, db, followups, sync, scheduler, contacts, drafts_api, evalmetrics, evalset, eval_api, api):
        importlib.reload(m)
    with TestClient(api.app) as c:
        yield c, db, evalset


EMAILS = [  # (kind, subject, sender, body)
    ("matched", "Thank you for applying to Acme!", "Acme <no-reply@us.greenhouse-mail.io>", "Thanks for applying to the Software Engineer role at Acme."),
    ("matched", "Update on your Zeta application", "Zeta Careers <no-reply@zeta.com>", "Unfortunately we will not be moving forward with your application."),
    ("matched", "Jobs you might like", "LinkedIn <jobs-listings@linkedin.com>", "10 new jobs for you"),
    ("matched", "Your interview", "Recruiting <recruiting@acme.com>", "Please let us know a time for your technical interview"),
    ("matched", "We received your application", "Beta Jobs <jobs@beta.com>", "Thank you. We have received your application for Data Analyst."),
    ("unmatched", "50% off everything", "Shop <deals@shop.com>", "Big sale this weekend"),
    ("unmatched", "Re: next steps", "Hiring <people@startup.io>", "Thanks for your time. We would like to schedule a call."),
]


def seed(db, evalset):
    with db.session() as conn:
        for n, (kind, subj, snd, body) in enumerate(EMAILS):
            conn.execute("INSERT INTO eval_items (gmail_id, kind, subject, sender, body) VALUES (?,?,?,?,?)", (f"g{n}", kind, subj, snd, body))
    evalset._predict_pending()


def test_predict_label_report_and_markdown(client):
    c, db, evalset = client
    seed(db, evalset)
    s = c.get("/api/eval/summary").json()
    assert (s["total"], s["predicted"], s["labeled"]) == (7, 7, 0)

    # label by walking the queue, like the UI: accept the prediction except where we know better
    truth = {"Thank you for applying to Acme!": dict(is_job=True, company="Acme", role="Software Engineer", status="Applied"),
             "Update on your Zeta application": dict(is_job=True, company="Zeta", role="", status="Rejected"),
             "Jobs you might like": dict(is_job=False),
             "We received your application": dict(is_job=True, company="Beta Jobs", role="Data Analyst", status="Applied"),
             "Your interview": dict(is_job=True, company="Acme", role="", status="Interview"),
             "50% off everything": dict(is_job=False),
             "Re: next steps": dict(is_job=True, company="Startup", role="", status="Interview")}      # a real email the search never matched
    seen = 0
    while True:
        item = c.get("/api/eval/next").json()["item"]
        if not item:
            break
        assert item["pred"] is not None and "body" in item
        assert c.post(f"/api/eval/{item['id']}/label", json=truth[item["subject"]]).status_code == 200
        seen += 1
    assert seen == 7 and c.get("/api/eval/summary").json()["labeled"] == 7
    assert c.post("/api/eval/1/label", json={"is_job": True, "status": "Hired"}).status_code == 422

    with db.session() as conn:
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('eval_universe', ?)", (json.dumps({"matched": 40, "unmatched": 200}),))
    r = c.get("/api/eval/report").json()
    assert r["n_matched_labeled"] == 5 and r["n_unmatched_labeled"] == 2
    det = r["detection"]
    assert det["precision"] == 1.0                                  # nothing it called a job was wrong
    assert r["extraction"]["status"]["n"] == 4                      # four real application emails among the matched
    assert r["search"]["unmatched_sample"]["k"] == 1                # the search missed one real email in the sample
    assert 0 < r["search"]["recall"] < 1
    assert r["rules_only"]["detection"]["tp"] >= 0 and "wild" in r

    from jobtracker import evalset as es
    with db.session() as conn:
        md = es.markdown(es.report(conn))
        err = es.errors_markdown(conn)
    assert "## Is it an application email?" in md and "95% CI" in md and "Confusion matrix" in md and "search recall" in md
    assert "Acme" not in md and "greenhouse" not in md              # the committable report has numbers only, never email text
    assert err.startswith("# Evaluation errors (private")


def test_hot_predicate_and_stratified_report(client):
    c, db, evalset = client
    assert evalset.is_hot("Application update", "Acme <no-reply@us.greenhouse-mail.io>")          # recruiting system
    assert evalset.is_hot("Next steps for your candidacy", "Jo <jo@startup.io>")                  # job words in the subject
    assert not evalset.is_hot("50% off everything", "Shop <deals@shop.com>")
    assert not evalset.is_hot("Jobs you might like", "LinkedIn <jobs-listings@linkedin.com>")      # job boards are blocklisted noise

    with db.session() as conn:
        rows = [("matched", "Thank you for applying to Acme!", "Acme <no-reply@us.greenhouse-mail.io>", "Thanks for applying to the SWE role at Acme.")] * 5
        rows += [("unmatched", "50% off", "Shop <deals@shop.com>", "sale")] * 4
        rows += [("unmatched_hot", "Re: next steps for your application", "Hiring <people@startup.io>", "We would like to schedule a call."),
                 ("unmatched_hot", "Career fair reminder", "Events <hi@school.edu>", "Join us")]
        for n, (kind, subj, snd, body) in enumerate(rows):
            conn.execute("INSERT INTO eval_items (gmail_id, kind, subject, sender, body) VALUES (?,?,?,?,?)", (f"s{n}", kind, subj, snd, body))
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('eval_universe', ?)",
                     (json.dumps({"matched": 50, "unmatched": 400, "n_hot": 2, "n_cold": 398}),))
    evalset._predict_pending()
    s = c.get("/api/eval/summary").json()
    assert s["hot_total"] == 2 and s["hot_labeled"] == 0

    while True:
        item = c.get("/api/eval/next").json()["item"]
        if not item:
            break
        job = item["subject"].startswith(("Thank you for applying", "Re: next steps"))
        c.post(f"/api/eval/{item['id']}/label", json=dict(is_job=job, company="Acme" if "Acme" in item["subject"] else "Startup", status="Applied" if job else None))
    r = c.get("/api/eval/report").json()["search"]
    assert r["stratified"] and r["hot"]["exact"] and r["hot"]["job"] == 1                          # one real email hiding among the 2 suspects
    assert r["cold"]["labeled"] == 4 and r["cold"]["job"] == 0 and r["estimated_missed"] == 1

    from jobtracker import evalset as es
    with db.session() as conn:
        md = es.markdown(es.report(conn))
    assert "I read the subject and sender of **all 400**" in md and "every suspect was checked" in md and "Shop" not in md


def test_evaluation_tools_do_not_exist_unless_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "off.db"))
    monkeypatch.setenv("AUTO_SYNC_MINUTES", "0")
    monkeypatch.setenv("DIGEST_ENABLED", "0")
    monkeypatch.setenv("ENABLE_EVAL", "0")
    from jobtracker import config, db, followups, sync, scheduler, contacts, drafts_api, evalmetrics, evalset, eval_api, api
    for m in (config, db, followups, sync, scheduler, contacts, drafts_api, evalmetrics, evalset, eval_api, api):
        importlib.reload(m)
    with TestClient(api.app) as c:
        assert c.get("/api/features").json()["eval"] is False
        for method, path in (("get", "/api/eval/summary"), ("get", "/api/eval/next"), ("get", "/api/eval/report"), ("post", "/api/eval/sample"),
                             ("post", "/api/eval/scan"), ("post", "/api/eval/predict"), ("delete", "/api/eval/data"), ("get", "/api/eval/labeled")):
            r = getattr(c, method)(path)
            assert r.status_code == 404 and "ENABLE_EVAL" in r.json()["detail"], path           # nothing is stored or reachable
        assert c.get("/api/applications").status_code == 200                                      # the rest of the app is unaffected


def test_heldout_is_frozen_blind_and_kept_out_of_the_tuning_numbers(client, monkeypatch):
    c, db, evalset = client
    from jobtracker import heldout
    with db.session() as conn, pytest.raises(ValueError, match="Freeze"):
        heldout._require_freeze(conn)                                                      # nothing may be sampled before the freeze
    assert c.post("/api/eval/heldout/freeze", json={"cutoff": "2026-10-11"}).json()["unchanged"]
    with db.session() as conn:
        conn.execute("INSERT INTO eval_items (gmail_id, kind, subject, sender, body, split) VALUES ('h1','matched',?,?,?,'heldout_new')",
                     ("Thank you for applying to Acme!", "Acme <no-reply@us.greenhouse-mail.io>", "Thanks for applying to the Software Engineer role at Acme."))
    evalset._predict_pending()

    assert c.get("/api/eval/next?split=tuning").json()["item"] is None                    # held-out mail never appears in normal labeling
    guess = c.get("/api/eval/next?split=heldout&blind=true").json()["item"]["pred"]
    assert guess["is_job"] is None and guess["status"] is None and guess["company"]       # blind: company/role to correct, no verdict
    item_id = c.get("/api/eval/next?split=heldout").json()["item"]["id"]
    c.post(f"/api/eval/{item_id}/label", json=dict(is_job=True, company="Acme", role="Software Engineer", status="Applied", blind=True))

    rep = c.get("/api/eval/heldout/report").json()
    assert rep["heldout"]["n"] == 1 and rep["progress"]["heldout_new"]["blind"] == 1
    with db.session() as conn:
        assert evalset._labeled(conn, "matched") == []                                    # the in-sample report doesn't include it
    assert c.post("/api/eval/heldout/freeze", json={"cutoff": "2026-10-12"}).status_code == 409   # frozen for good once labels exist

    monkeypatch.setattr(heldout, "_hashes", lambda: {f: "changed" for f in heldout.FREEZE_FILES})
    st = c.get("/api/eval/heldout").json()["freeze"]
    assert not st["unchanged"] and st["changed_files"]
    with db.session() as conn:
        with pytest.raises(ValueError, match="changed"):
            heldout._require_freeze(conn)                                                  # a changed parser is refused
        assert "no longer matches" in heldout.markdown(heldout.report(conn))
