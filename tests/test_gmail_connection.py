"""An expired or revoked Gmail connection is a normal state to explain, never an internal server error."""
import json

import pytest
from google.auth.exceptions import RefreshError

from test_milestones import env  # noqa: F401  (the app + temp database fixture)


class DeadCreds:
    valid = False
    expired = True
    refresh_token = "x"

    def refresh(self, request):
        raise RefreshError("invalid_grant: Token has been expired or revoked.")


@pytest.fixture()
def dead_connection(env, tmp_path, monkeypatch):
    c, db, m, sync = env
    from jobtracker import gmail
    token = tmp_path / "gmail_connection.json"
    token.write_text("{}")
    monkeypatch.setattr(gmail.config, "GMAIL_TOKEN_FILE", token)
    monkeypatch.setattr(gmail.Credentials, "from_authorized_user_file", staticmethod(lambda *a, **k: DeadCreds()))
    return c, db, gmail


def test_an_expired_connection_becomes_a_clear_message(dead_connection):
    c, db, gmail = dead_connection
    with pytest.raises(gmail.AuthRequired) as e:
        gmail.authorize()
    assert "expired or was revoked" in str(e.value) and "python -m jobtracker.gmail" in str(e.value)
