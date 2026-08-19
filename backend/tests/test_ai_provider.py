import asyncio
import sys
from types import SimpleNamespace

import pytest

from backend.app import proxy
from backend.app.services import ai_provider


class StatusError(Exception):
    def __init__(self, status_code):
        self.status_code = status_code


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (401, "provider_auth_failed", False),
        (403, "model_unavailable", False),
        (404, "model_unavailable", False),
        (429, "provider_rate_limited", True),
        (503, "provider_unavailable", True),
    ],
)
def test_provider_error_classification(status, code, retryable):
    error = ai_provider._classify_provider_error(StatusError(status))
    assert error.code == code
    assert error.retryable is retryable


def test_deepseek_uses_official_root_base_url():
    assert ai_provider.PROVIDER_DEFAULTS["deepseek"]["base_url"] == (
        "https://api.deepseek.com"
    )


def test_missing_dependency_has_stable_error(monkeypatch):
    async def missing(*args, **kwargs):
        raise ImportError("missing", name="openai")

    monkeypatch.setattr(ai_provider, "_openai_compat_complete", missing)
    with pytest.raises(ai_provider.AIProviderError) as caught:
        asyncio.run(
            ai_provider.chat_complete(
                "openai", "key", "https://api.openai.com/v1", "m", []
            )
        )
    assert caught.value.code == "dependency_missing"
    assert "openai" not in caught.value.message.lower()


def test_openai_client_is_closed_when_request_fails(monkeypatch):
    state = {"closed": False}

    class Completions:
        async def create(self, **kwargs):
            raise StatusError(401)

    class Client:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=Completions())

        async def close(self):
            state["closed"] = True

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(AsyncOpenAI=Client))
    with pytest.raises(StatusError):
        asyncio.run(
            ai_provider._openai_compat_complete(
                "key", "https://api.openai.com/v1", "model", [], None
            )
        )
    assert state["closed"] is True


def test_http_client_is_closed_when_sdk_construction_fails(monkeypatch):
    clients = []

    class HttpClient:
        def __init__(self, **kwargs):
            self.is_closed = False
            clients.append(self)

        async def aclose(self):
            self.is_closed = True

    class BrokenClient:
        def __init__(self, **kwargs):
            raise RuntimeError("constructor failed")

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(AsyncOpenAI=BrokenClient))
    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(AsyncClient=HttpClient))
    with pytest.raises(RuntimeError):
        asyncio.run(
            ai_provider._openai_compat_complete(
                "key", "https://api.openai.com/v1", "model", [], None
            )
        )
    assert clients[0].is_closed is True


def test_chat_complete_redacts_provider_error(monkeypatch):
    async def rejected(*args, **kwargs):
        raise StatusError(401)

    monkeypatch.setattr(ai_provider, "_openai_compat_complete", rejected)
    with pytest.raises(ai_provider.AIProviderError) as caught:
        asyncio.run(
            ai_provider.chat_complete(
                "openai", "secret", "https://api.openai.com/v1", "m", []
            )
        )
    assert caught.value.code == "provider_auth_failed"
    assert "secret" not in caught.value.message


@pytest.mark.parametrize(
    ("provider", "completion_name"),
    [
        ("openai", "_openai_compat_complete"),
        ("claude", "_claude_complete"),
    ],
)
def test_chat_complete_rewrites_loopback_proxy_inside_docker(
    monkeypatch, provider, completion_name
):
    captured = {}

    async def complete(*args):
        captured["proxy_url"] = args[-1]
        return "ok"

    monkeypatch.setenv("DOCKER_CONTAINER", "1")
    monkeypatch.setattr(ai_provider, completion_name, complete)

    result = asyncio.run(
        ai_provider.chat_complete(
            provider,
            "key",
            "https://api.example.com" if provider != "claude" else None,
            "model",
            [{"role": "user", "content": "test"}],
            "socks5://127.0.0.1:1080",
        )
    )

    assert result == "ok"
    assert captured["proxy_url"] == "socks5://host.docker.internal:1080"


@pytest.mark.parametrize(
    ("provider", "completion_name"),
    [
        ("openai", "_openai_compat_complete"),
        ("claude", "_claude_complete"),
    ],
)
def test_chat_complete_keeps_proxy_unchanged_outside_docker(
    monkeypatch, provider, completion_name
):
    captured = {}

    async def complete(*args):
        captured["proxy_url"] = args[-1]
        return "ok"

    monkeypatch.setattr(proxy.os.path, "exists", lambda path: False)
    monkeypatch.delenv("DOCKER_CONTAINER", raising=False)
    monkeypatch.setattr(ai_provider, completion_name, complete)

    result = asyncio.run(
        ai_provider.chat_complete(
            provider,
            "key",
            "https://api.example.com" if provider != "claude" else None,
            "model",
            [],
            "http://localhost:7897",
        )
    )

    assert result == "ok"
    assert captured["proxy_url"] == "http://localhost:7897"
