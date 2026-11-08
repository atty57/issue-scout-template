"""Use a fictional profile for offline tests, never a developer's personal profile."""
import os
from pathlib import Path

import pytest


os.environ.setdefault("SCOUT_PROFILE", str(Path(__file__).parent / "profile.example.json"))


@pytest.fixture(autouse=True)
def isolated_email_configuration(monkeypatch):
    for name in ("SCOUT_RECIPIENT_EMAIL", "SCOUT_EMAIL_RELAY_URL", "ACTIONS_ID_TOKEN_REQUEST_URL", "ACTIONS_ID_TOKEN_REQUEST_TOKEN"):
        monkeypatch.delenv(name, raising=False)
