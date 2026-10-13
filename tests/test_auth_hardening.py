"""Sign-in throttling per device, sessions tied to the password, and 'sign out everywhere'."""
import pytest
from starlette.requests import Request

from test_auth import PASSWORD, make_client


def test_one_devices_wrong_passwords_do_not_lock_out_another(tmp_path, monkeypatch):
    client, auth = make_client(tmp_path, monkeypatch)
    auth.reset_throttle()
    with client as c:
        for _ in range(5):                       # a stranger on the tailnet, as the proxy reports them
            assert c.post("/login", json={"password": "wrong"}, headers={"X-Forwarded-For": "100.64.0.9"}).status_code == 401
        assert c.post("/login", json={"password": PASSWORD}, headers={"X-Forwarded-For": "100.64.0.9"}).status_code == 429
        mine = c.post("/login", json={"password": PASSWORD}, headers={"X-Forwarded-For": "100.102.51.101"})
        assert mine.status_code == 200           # you are not locked out by their attempts


def test_global_cap_still_stops_a_client_that_rotates_its_claimed_address(tmp_path, monkeypatch):
    client, auth = make_client(tmp_path, monkeypatch)
    auth.reset_throttle()
    with client as c:
        for i in range(30):                      # every guess claims a different source
            assert c.post("/login", json={"password": "wrong"}, headers={"X-Forwarded-For": f"100.64.1.{i}"}).status_code == 401
        blocked = c.post("/login", json={"password": PASSWORD}, headers={"X-Forwarded-For": "100.64.2.1"})
        assert blocked.status_code == 429        # brute force is capped no matter what the client claims
    auth.reset_throttle()


def test_forged_forwarded_for_values_fall_back_to_the_connection_address(tmp_path, monkeypatch):
    client, auth = make_client(tmp_path, monkeypatch)

    def request(header):
        headers = [(b"x-forwarded-for", header.encode())] if header else []
        return Request({"type": "http", "headers": headers, "client": ("127.0.0.1", 1234)})
    assert auth.source_key(request("100.102.51.101")) == "100.102.51.101"
    assert auth.source_key(request("100.64.0.1, 10.0.0.2")) == "100.64.0.1"
    assert auth.source_key(request("not-an-ip; DROP TABLE x")) == "127.0.0.1"
    assert auth.source_key(request("")) == "127.0.0.1"


def test_changing_the_password_or_rotating_the_key_ends_every_session(tmp_path, monkeypatch):
    client, auth = make_client(tmp_path, monkeypatch)
    with client as c:
        c.post("/login", json={"password": PASSWORD})
        phone = c.cookies.get(auth.COOKIE)
        assert c.get("/api/applications").status_code == 200

        monkeypatch.setattr(auth.config, "APP_PASSWORD", "a brand new passphrase")   # you changed the password in .env
        assert not auth.valid_token(phone) and c.get("/api/applications").status_code == 401
        monkeypatch.setattr(auth.config, "APP_PASSWORD", PASSWORD)
        assert auth.valid_token(phone)                                               # restored: the cookie is valid again

        auth.rotate_key()                                                            # "sign out everywhere"
        assert not auth.valid_token(phone) and c.get("/api/applications").status_code == 401


def test_sign_out_everywhere_requires_a_session_and_ends_all_of_them(tmp_path, monkeypatch):
    client, auth = make_client(tmp_path, monkeypatch)
    with client as c:
        assert c.post("/logout-all").status_code in (303, 401)                       # refused while signed out
        c.post("/login", json={"password": PASSWORD})
        other_device = auth.make_token()
        assert auth.valid_token(other_device)
        assert c.post("/logout-all").status_code == 200
        assert not auth.valid_token(other_device) and c.get("/api/applications").status_code == 401
        assert c.post("/login", json={"password": PASSWORD}).status_code == 200      # signing in again works


def test_expired_cookie_is_rejected_for_being_expired(tmp_path, monkeypatch):
    import time
    client, auth = make_client(tmp_path, monkeypatch)
    with client:
        past, future = str(int(time.time()) - 5), str(int(time.time()) + 60)
        assert auth.valid_token(f"{future}.{auth._sig(future)}")                     # same construction, in date: accepted
        assert not auth.valid_token(f"{past}.{auth._sig(past)}")                     # correctly signed, but out of date: refused
