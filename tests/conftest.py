"""Shared test setup: tests must never depend on the developer's own .env.

config.py loads .env at import time, so any setting a test doesn't pin would otherwise come from the real file
(a login password, an API key, the evaluation switch...). These defaults are applied before each test; a test or
fixture that needs a different value just sets it afterwards.
"""
import pytest

SAFE_DEFAULTS = {
    "APP_PASSWORD": "",
    "ENABLE_EVAL": "0",
    "GROQ_API_KEY": "",
    "LINKEDIN_NOTE_MAX": "200",
    "AUTO_SYNC_MINUTES": "0",
    "DIGEST_ENABLED": "0",
    "TIMEZONE": "",
    "BLOCKED_SENDERS": "",
}


@pytest.fixture(autouse=True)
def _isolate_from_real_env(monkeypatch):
    for key, value in SAFE_DEFAULTS.items():
        monkeypatch.setenv(key, value)
