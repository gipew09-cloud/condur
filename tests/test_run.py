from app.run import service_role


def test_service_role_defaults_to_main(monkeypatch):
    monkeypatch.delenv("SERVICE_ROLE", raising=False)
    monkeypatch.delenv("RAILWAY_SERVICE_NAME", raising=False)

    assert service_role() == "main"


def test_service_role_uses_explicit_env(monkeypatch):
    monkeypatch.setenv("SERVICE_ROLE", "egts")
    monkeypatch.setenv("RAILWAY_SERVICE_NAME", "condur")

    assert service_role() == "egts"


def test_service_role_detects_egts_service_name(monkeypatch):
    monkeypatch.delenv("SERVICE_ROLE", raising=False)
    monkeypatch.setenv("RAILWAY_SERVICE_NAME", "egts-receiver")

    assert service_role() == "egts"


def test_database_url_validator_strips_accidental_cyrillic_k():
    from app.config import Settings

    settings = Settings(
        database_url="кpostgresql://user:pass@localhost:5432/db",
        owner_bot_token="owner",
        driver_bot_token="driver",
    )

    assert settings.database_url == "postgresql+asyncpg://user:pass@localhost:5432/db"
