from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app import database
from backend.app.routes import ai_config
from backend.app.services import ai_config_validation
from backend.app.services.ai_provider import AIProviderError


def _client(monkeypatch):
    monkeypatch.setattr(
        ai_config_validation.socket,
        "getaddrinfo",
        lambda host, port: [(2, 1, 6, "", ("93.184.216.34", 0))],
    )
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    database.Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)

    def override_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr(ai_config, "encrypt", lambda value: f"enc:{value}")
    monkeypatch.setattr(ai_config, "decrypt", lambda value: value.removeprefix("enc:"))
    app = FastAPI()
    app.include_router(ai_config.router, prefix="/api/ai")
    app.dependency_overrides[ai_config.get_db] = override_db
    return TestClient(app), session_factory


def test_create_validates_and_does_not_return_key(monkeypatch):
    client, sessions = _client(monkeypatch)
    response = client.post(
        "/api/ai/create",
        json={
            "name": " OpenAI ",
            "provider": "openai",
            "api_key": "top-secret",
            "model_name": "gpt-4o-mini",
        },
    )
    assert response.status_code == 200

    listed = client.get("/api/ai/list").json()
    assert listed[0]["name"] == "OpenAI"
    assert listed[0]["api_key_set"] is True
    assert "top-secret" not in str(listed)

    with sessions() as session:
        assert session.query(database.AIConfig).one().api_key_enc == "enc:top-secret"


def test_draft_test_reuses_key_without_writing(monkeypatch):
    client, sessions = _client(monkeypatch)
    with sessions() as session:
        config = database.AIConfig(
            name="Saved",
            provider="openai",
            api_key_enc="enc:saved-secret",
            model_name="gpt-4o-mini",
        )
        session.add(config)
        session.commit()
        config_id = config.id

    observed = {}

    async def fake_test(config, proxy):
        observed.update(config)
        return "1"

    monkeypatch.setattr(ai_config, "test_connection", fake_test)
    response = client.post(
        "/api/ai/test-draft",
        json={
            "config_id": config_id,
            "name": "Unsaved name",
            "provider": "openai",
            "api_key": "",
            "model_name": "gpt-4o-mini",
        },
    )
    assert response.status_code == 200
    assert observed["api_key"] == "saved-secret"
    with sessions() as session:
        assert session.get(database.AIConfig, config_id).name == "Saved"


def test_update_with_empty_key_keeps_existing_secret(monkeypatch):
    client, sessions = _client(monkeypatch)
    with sessions() as session:
        config = database.AIConfig(
            name="Saved",
            provider="openai",
            api_key_enc="enc:saved-secret",
            model_name="gpt-4o-mini",
        )
        session.add(config)
        session.commit()
        config_id = config.id

    response = client.put(
        f"/api/ai/{config_id}",
        json={
            "name": "Updated",
            "provider": "openai",
            "api_key": "",
            "model_name": "gpt-4o-mini",
        },
    )
    assert response.status_code == 200
    with sessions() as session:
        updated = session.get(database.AIConfig, config_id)
        assert updated.name == "Updated"
        assert updated.api_key_enc == "enc:saved-secret"


def test_provider_change_does_not_reuse_existing_secret(monkeypatch):
    client, sessions = _client(monkeypatch)
    with sessions() as session:
        config = database.AIConfig(
            name="Saved",
            provider="openai",
            api_key_enc="enc:saved-secret",
            model_name="gpt-4o-mini",
        )
        session.add(config)
        session.commit()
        config_id = config.id

    response = client.put(
        f"/api/ai/{config_id}",
        json={
            "name": "Changed provider",
            "provider": "deepseek",
            "api_key": "",
            "model_name": "deepseek-v4-pro",
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_config"
    with sessions() as session:
        unchanged = session.get(database.AIConfig, config_id)
        assert unchanged.provider == "openai"
        assert unchanged.api_key_enc == "enc:saved-secret"


def test_provider_change_to_local_clears_existing_secret(monkeypatch):
    client, sessions = _client(monkeypatch)
    with sessions() as session:
        config = database.AIConfig(
            name="Saved",
            provider="openai",
            api_key_enc="enc:saved-secret",
            model_name="gpt-4o-mini",
        )
        session.add(config)
        session.commit()
        config_id = config.id

    response = client.put(
        f"/api/ai/{config_id}",
        json={
            "name": "Local gateway",
            "provider": "custom",
            "api_key": "",
            "base_url": "http://192.168.1.20:8000/v1",
            "model_name": "local-model",
        },
    )

    assert response.status_code == 200
    with sessions() as session:
        updated = session.get(database.AIConfig, config_id)
        assert updated.provider == "custom"
        assert updated.api_key_enc is None


def test_missing_activate_does_not_clear_current_active_config(monkeypatch):
    client, sessions = _client(monkeypatch)
    with sessions() as session:
        config = database.AIConfig(name="Active", provider="ollama", is_active=True)
        session.add(config)
        session.commit()
        config_id = config.id

    response = client.post("/api/ai/999/activate")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "config_not_found"
    with sessions() as session:
        assert session.get(database.AIConfig, config_id).is_active is True


def test_provider_failure_is_stable_and_redacted(monkeypatch):
    client, sessions = _client(monkeypatch)
    with sessions() as session:
        config = database.AIConfig(
            name="OpenAI",
            provider="openai",
            api_key_enc="enc:secret-value",
            model_name="gpt-4o-mini",
        )
        session.add(config)
        session.commit()
        config_id = config.id

    async def rejected(config, proxy):
        raise AIProviderError("provider_auth_failed", "API Key 无效或无权访问该服务")

    monkeypatch.setattr(ai_config, "test_connection", rejected)
    response = client.post(f"/api/ai/{config_id}/test")
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail == {
        "code": "provider_auth_failed",
        "message": "API Key 无效或无权访问该服务",
        "retryable": False,
    }
    assert "secret-value" not in response.text


def test_invalid_draft_is_rejected_before_provider_call(monkeypatch):
    client, _ = _client(monkeypatch)

    async def must_not_run(config, proxy):
        raise AssertionError("provider should not be called")

    monkeypatch.setattr(ai_config, "test_connection", must_not_run)
    response = client.post(
        "/api/ai/test-draft",
        json={
            "name": "Bad",
            "provider": "deepseek",
            "api_key": "secret",
            "base_url": "http://127.0.0.1:8000/v1",
            "model_name": "deepseek-chat",
        },
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_config"


def test_failed_draft_test_does_not_persist_or_expose_key(monkeypatch):
    client, sessions = _client(monkeypatch)

    async def rejected(config, proxy):
        raise AIProviderError("provider_auth_failed", "API Key 无效或无权访问该服务")

    monkeypatch.setattr(ai_config, "test_connection", rejected)
    response = client.post(
        "/api/ai/test-draft",
        json={
            "name": "Unsaved DeepSeek",
            "provider": "deepseek",
            "api_key": "draft-top-secret",
            "base_url": "https://api.deepseek.com/v1",
            "model_name": "deepseek-chat",
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == {
        "code": "provider_auth_failed",
        "message": "API Key 无效或无权访问该服务",
        "retryable": False,
    }
    assert "draft-top-secret" not in response.text
    with sessions() as session:
        assert session.query(database.AIConfig).count() == 0
