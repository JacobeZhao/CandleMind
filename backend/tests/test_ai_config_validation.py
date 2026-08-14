import pytest

from backend.app.services import ai_config_validation as validation


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    monkeypatch.setattr(
        validation.socket,
        "getaddrinfo",
        lambda host, port: [(2, 1, 6, "", ("93.184.216.34", 0))],
    )
    monkeypatch.delenv("CANDLEMIND_AI_BASE_URL_HOSTS", raising=False)


def test_cloud_provider_uses_official_https_host():
    config = validation.validate_ai_config(
        name=" DeepSeek ",
        provider="DEEPSEEK",
        api_key="secret",
        base_url="https://api.deepseek.com/v1/",
        model_name="deepseek-chat",
    )

    assert config.name == "DeepSeek"
    assert config.base_url == "https://api.deepseek.com/v1"

    with pytest.raises(validation.AIConfigValidationError, match="官方"):
        validation.validate_base_url("deepseek", "https://example.com/v1")
    with pytest.raises(validation.AIConfigValidationError, match="HTTPS"):
        validation.validate_base_url("deepseek", "http://api.deepseek.com/v1")


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://user:password@localhost:4000/v1",
        "http://localhost:4000/v1?token=x",
        "http://169.254.169.254/latest/meta-data",
    ],
)
def test_rejects_dangerous_or_unapproved_urls(url):
    with pytest.raises(validation.AIConfigValidationError):
        validation.validate_base_url("litellm", url)


def test_local_and_explicit_gateway_hosts_are_allowed(monkeypatch):
    assert validation.validate_base_url("ollama", "http://localhost:11434/v1")

    monkeypatch.setenv("CANDLEMIND_AI_BASE_URL_HOSTS", "gateway.internal")
    assert validation.validate_base_url("litellm", "https://gateway.internal/v1")


def test_cloud_key_and_model_are_required_but_ollama_key_is_optional():
    with pytest.raises(validation.AIConfigValidationError, match="API Key"):
        validation.validate_ai_config(
            name="OpenAI",
            provider="openai",
            api_key="",
            base_url=None,
            model_name="gpt-4o-mini",
        )

    config = validation.validate_ai_config(
        name="Ollama",
        provider="ollama",
        api_key=None,
        base_url=None,
        model_name=None,
    )
    assert config.api_key == ""
    assert config.model_name == "llama3"


def test_existing_key_is_reused_and_control_characters_are_rejected():
    config = validation.validate_ai_config(
        name="Saved",
        provider="openai",
        api_key="",
        existing_api_key="existing-secret",
        base_url=None,
        model_name="gpt-4o-mini",
    )
    assert config.api_key == "existing-secret"

    with pytest.raises(validation.AIConfigValidationError, match="模型"):
        validation.validate_ai_config(
            name="Bad",
            provider="openai",
            api_key="secret",
            base_url=None,
            model_name="bad\nmodel",
        )
