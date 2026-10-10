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


def test_report_needs_labels_first(client):
    c, db, evalset = client
    assert c.get("/api/eval/report").status_code == 409


def test_relabel_edit_and_unlabel(client):
    c, db, evalset = client
    seed(db, evalset)
    first = c.get("/api/eval/next").json()["item"]
    assert c.post(f"/api/eval/{first['id']}/label", json=dict(is_job=False)).status_code == 200      # a mislabel
    row = next(i for i in c.get("/api/eval/labeled").json() if i["id"] == first["id"])
    assert row["truth"]["is_job"] is False and row["corrected"] is True                            # differs from the pipeline's guess

    item = c.get(f"/api/eval/item/{first['id']}").json()["item"]
    assert item["truth"]["is_job"] is False and item["pred"]["is_job"] is True                     # the UI can prefill from the saved label
    c.post(f"/api/eval/{first['id']}/label", json=dict(is_job=True, company="Acme", role="Software Engineer", status="Applied"))
    again = next(i for i in c.get("/api/eval/labeled").json() if i["id"] == first["id"])
    assert again["truth"]["is_job"] is True and again["truth"]["company"] == "Acme"                # overwritten, not duplicated
    assert c.get("/api/eval/summary").json()["labeled"] == 1

    assert c.post(f"/api/eval/{first['id']}/unlabel").json()["labeled"] == 0
    assert c.get("/api/eval/next").json()["item"]["id"] == first["id"]                             # back in the queue
    assert c.post("/api/eval/9999/unlabel").status_code == 404 and c.get("/api/eval/item/9999").status_code == 404
