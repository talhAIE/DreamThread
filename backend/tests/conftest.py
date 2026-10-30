"""Test configuration for backend tests.
Ensures the project's backend directory (containing the `app` package) is on PYTHONPATH
so that `import app` works when tests are executed from the repository root.
"""
import pathlib
import sys

import pytest

# Add the backend directory (which contains the "app" package) to PYTHONPATH.
repo_root = pathlib.Path(__file__).resolve().parents[2]
backend_path = repo_root / "backend"
if str(backend_path) not in sys.path:
    sys.path.insert(0, str(backend_path))

from app.config import Settings  # noqa: E402


@pytest.fixture(autouse=True)
def isolate_application_environment(monkeypatch):
    """Unset all environment variables defined in Settings for isolation."""
    for field in Settings.model_fields:
        monkeypatch.delenv(field.upper(), raising=False)
