"""Foundation contract tests. Database networking is isolated here."""

import pytest
from fastapi import Query
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings
from app.main import create_app

URL = "postgresql+psycopg://user:secret-password@localhost:5432/test"


class FakeDatabase:
    def __init__(self, result="ok"):
        self.result = result
        self.closed = False

    async def probe(self):
        return self.result

    async def close(self):
        self.closed = True


def settings(**values):
    return Settings(_env_file=None, database_url=URL, **values)


def test_configuration_validation_and_secrets():
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, database_url="invalid-secret")
    assert "invalid-secret" not in str(error.value)
    assert "secret-password" not in repr(settings())
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
    with pytest.raises(ValidationError):
        settings(cors_origins=["*"])
    with pytest.raises(ValidationError):
        settings(cors_origins=["https://example.com/path"])
    with pytest.raises(ValidationError):
        settings(app_env="production")


def test_lifecycle_openapi_and_request_ids():
    db = FakeDatabase()
    with TestClient(create_app(settings(), db)) as client:
        response = client.get("/health/live", headers={"X-Request-ID": "local-check_1"})
        assert response.json() == {"status": "alive"}
        assert response.headers["x-request-id"] == "local-check_1"
        response = client.get("/health/live", headers={"X-Request-ID": "invalid id"})
        assert len(response.headers["x-request-id"]) == 32
        schema = client.get("/openapi.json").json()
        assert "/health/live" in schema["paths"]
        assert "/health/ready" in schema["paths"]
        assert client.get("/docs").status_code == 200
    assert db.closed


@pytest.mark.parametrize("result", ["schema_mismatch", "unavailable"])
def test_unready_is_safe_and_liveness_independent(result):
    with TestClient(create_app(settings(), FakeDatabase(result))) as client:
        assert client.get("/health/live").status_code == 200
        response = client.get("/health/ready")
        assert response.status_code == 503
        assert response.json()["error"]["details"] == {"database": result}
        assert response.json()["error"]["request_id"] == response.headers["x-request-id"]
        assert "secret-password" not in response.text


def test_ready_provider_configuration_does_not_claim_live_verification():
    config = settings(
        nebius_api_key="private-key", nebius_extraction_model="tested-id",
        nebius_reasoning_model="tested-id",
    )
    with TestClient(create_app(config, FakeDatabase())) as client:
        response = client.get("/health/ready")
        assert response.status_code == 200
        assert response.json()["providers"]["nebius_inference"] == "configured_unverified"
        assert "private-key" not in response.text
        assert "tested-id" not in response.text


def test_uniform_errors_and_cors(caplog):
    app = create_app(settings(), FakeDatabase())

    @app.get("/test-validation")
    async def validate(number: int = Query()):
        return number

    @app.get("/test-failure")
    async def fail():
        raise RuntimeError("secret-password private journal text")

    with TestClient(app) as client:
        for path, status, code in [
            ("/missing", 404, "http_404"),
            ("/test-validation?number=private-secret", 422, "validation_error"),
            ("/test-failure", 500, "internal_error"),
        ]:
            response = client.get(path, headers={"Origin": "http://localhost:3000"})
            assert response.status_code == status
            assert response.json()["error"]["code"] == code
            assert response.json()["error"]["request_id"] == response.headers["x-request-id"]
            assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
            for secret in ("secret-password", "private-secret", "private journal text"):
                assert secret not in response.text
                assert secret not in caplog.text
        assert client.post("/health/live").json()["error"]["code"] == "http_405"
        response = client.get("/health/live", headers={"Origin": "https://attacker.invalid"})
        assert "access-control-allow-origin" not in response.headers
        response = client.options("/health/live", headers={
            "Origin": "http://localhost:3000", "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization,x-request-id",
        })
        assert response.status_code == 200


def test_factory_scrubs_startup_error(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "secret-bad-url")
    with pytest.raises(RuntimeError) as error:
        create_app()
    assert "database_url" in str(error.value)
    assert "secret-bad-url" not in str(error.value)


def test_production_configuration_accepts_only_complete_secure_settings():
    values = {
        "_env_file": None,
        "app_env": "production",
        "database_url": URL + "?sslmode=verify-full",
        "cors_origins": ["https://dreamthread.example"],
        "expected_schema_revision": "phase2-revision",
        "supabase_url": "https://project.supabase.co",
        "supabase_publishable_key": "test-publishable",
        "nebius_api_key": "test-secret",
        "nebius_extraction_model": "test-extraction-id",
        "nebius_reasoning_model": "test-reasoning-id",
        "nebius_embedding_model": "test-embedding-id",
        "embedding_dimensions": 128,
    }
    assert Settings(**values).app_env == "production"
    for key, bad in [
        ("cors_origins", ["http://localhost:3000"]),
        ("expected_schema_revision", "unversioned"),
        ("database_url", URL),
        ("embedding_dimensions", 0),
    ]:
        with pytest.raises(ValidationError):
            Settings(**{**values, key: bad})
