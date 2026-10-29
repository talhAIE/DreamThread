"""Unit/integration tests never consume a developer's actual provider credentials."""

import pytest

from app.config import Settings


@pytest.fixture(autouse=True)
def isolate_application_environment(monkeypatch):
    for field in Settings.model_fields:
        monkeypatch.delenv(field.upper(), raising=False)
