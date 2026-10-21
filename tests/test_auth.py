import importlib
import time

import pytest

TestClient = pytest.importorskip("fastapi.testclient").TestClient
PASSWORD = "correct horse battery staple"


def make_client(tmp_path, monkeypatch, password=PASSWORD):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "a.db"))
    monkeypatch.setenv("AUTO_SYNC_MINUTES", "0")
    monkeypatch.setenv("DIGEST_ENABLED", "0")
    monkeypatch.setenv("APP_PASSWORD", password)
    from jobtracker import config, db, followups, sync, scheduler, contacts, drafts_api, evalmetrics, evalset, eval_api, milestones, milestones_api, digest, auth, api
    for m in (config, db, followups, sync, scheduler, contacts, drafts_api, evalmetrics, evalset, eval_api, milestones, milestones_api, digest, auth, api):
        importlib.reload(m)
    monkeypatch.setattr(time, "sleep", lambda s: None)                  # the failed-login delay would only slow the tests down
    return TestClient(api.app, follow_redirects=False), auth


def test_everything_is_locked_until_you_sign_in(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch)[0] as c:
        r = c.get("/")
        assert r.status_code == 303 and r.headers["location"] == "/login"
        assert c.get("/static/index.html").status_code == 303                       # the UI code is behind the login too
        for path in ("/api/applications", "/api/contacts", "/api/resume", "/api/digest/status", "/api/stats"):
            assert c.get(path).status_code == 401, path
        assert c.post("/api/applications", json={"company": "X"}).status_code == 401
        page = c.get("/login")
        assert page.status_code == 200 and "Enter your password" in page.text and "type=\"password\"" in page.text


def test_sign_in_sets_a_cookie_that_unlocks_the_app_and_logout_removes_it(tmp_path, monkeypatch):
    client, auth = make_client(tmp_path, monkeypatch)
    with client as c:
        assert c.post("/login", json={"password": "nope"}).status_code == 401
        assert c.get("/api/applications").status_code == 401                         # a wrong guess grants nothing
        ok = c.post("/login", json={"password": PASSWORD})
        assert ok.status_code == 200 and "httponly" in ok.headers["set-cookie"].lower() and "samesite=lax" in ok.headers["set-cookie"].lower()
        assert c.get("/api/applications").status_code == 200 and c.get("/").status_code == 200
        assert c.get("/login").status_code == 303                                    # already signed in: bounce to the app
        c.post("/logout")
        assert c.get("/api/applications").status_code == 401


def test_cookie_is_signed_and_expires(tmp_path, monkeypatch):
    client, auth = make_client(tmp_path, monkeypatch)
    with client as c:
        good = auth.make_token()
        exp, sig = good.split(".")
        assert auth.valid_token(good)
        assert not auth.valid_token(f"{int(exp) + 999999}.{sig}")                    # changing the expiry breaks the signature
        assert not auth.valid_token(f"{exp}.{'0' * 64}") and not auth.valid_token("garbage") and not auth.valid_token(None)
        old = f"{int(time.time()) - 5}"
        import hashlib, hmac
        expired = f"{old}.{hmac.new(auth._key(), old.encode(), hashlib.sha256).hexdigest()}"
        assert not auth.valid_token(expired)                                         # correctly signed but out of date
        c.cookies.set(auth.COOKIE, f"{int(exp) + 5}.{sig}")
        assert c.get("/api/applications").status_code == 401


def test_cross_site_writes_are_refused_even_with_a_valid_cookie(tmp_path, monkeypatch):
    client, auth = make_client(tmp_path, monkeypatch)
    with client as c:
        c.post("/login", json={"password": PASSWORD})
        evil = c.post("/api/applications", json={"company": "Evil"}, headers={"Origin": "https://evil.example", "Host": "testserver"})
        assert evil.status_code == 403
        same = c.post("/api/applications", json={"company": "Fine"}, headers={"Origin": "http://testserver"})
        assert same.status_code == 201
