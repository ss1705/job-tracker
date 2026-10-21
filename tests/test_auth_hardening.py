"""Sign-in throttling per device, sessions tied to the password, and 'sign out everywhere'."""
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
