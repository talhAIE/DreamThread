import pytest
from pydantic import ValidationError

from app.config import Settings
from app.main import create_app


def settings(**overrides):
    return Settings(
        _env_file=None,
        database_url="postgresql://user:password@localhost/dreamthread",
        **overrides,
    )


def test_voice_is_disabled_and_configuration_cannot_enable_unverified_speech():
    assert settings().voice_enabled is False

    with pytest.raises(ValidationError, match="private storage and speech processing"):
        settings(voice_enabled=True)


def test_audio_endpoints_are_not_advertised_until_storage_and_asr_are_available():
    app = create_app(
        settings=settings(),
        database=object(),
        authenticator=object(),
    )

    assert not any("/audio" in path for path in app.openapi()["paths"])
