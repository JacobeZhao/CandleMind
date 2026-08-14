"""
Unified LLM provider interface.
OpenAI-compatible providers (deepseek/qwen/kimi/glm/minimax/gemini/openrouter/litellm/ollama)
all use the openai SDK with a custom base_url.
Claude uses the anthropic SDK separately.
"""
from typing import Optional

from loguru import logger

AI_REQUEST_TIMEOUT_SECONDS = 30


class AIProviderError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


def _classify_provider_error(exc: Exception) -> AIProviderError:
    if isinstance(exc, AIProviderError):
        return exc
    if isinstance(exc, (TimeoutError,)) or "timeout" in type(exc).__name__.lower():
        return AIProviderError("provider_timeout", "AI 服务请求超时", retryable=True)
    status = getattr(exc, "status_code", None)
    if status == 401:
        return AIProviderError("provider_auth_failed", "API Key 无效或无权访问该服务")
    if status in {403, 404}:
        return AIProviderError("model_unavailable", "所选 AI 模型不存在或当前账号无权访问")
    if status == 429:
        return AIProviderError("provider_rate_limited", "AI 服务请求频率受限，请稍后重试", retryable=True)
    if status is not None and status >= 500:
        return AIProviderError("provider_unavailable", "AI 服务暂时不可用", retryable=True)
    return AIProviderError("provider_unavailable", "无法连接 AI 服务", retryable=True)

PROVIDER_DEFAULTS = {
    "openai":     {"base_url": "https://api.openai.com/v1",                               "model": "gpt-4o-mini"},
    "deepseek":   {"base_url": "https://api.deepseek.com/v1",                             "model": "deepseek-v4-flash"},
    "qwen":       {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",       "model": "qwen-turbo"},
    "kimi":       {"base_url": "https://api.moonshot.cn/v1",                              "model": "moonshot-v1-8k"},
    "glm":        {"base_url": "https://open.bigmodel.cn/api/paas/v4",                    "model": "glm-4-flash"},
    "minimax":    {"base_url": "https://api.minimax.chat/v1",                             "model": "abab6.5s-chat"},
    "gemini":     {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai/","model": "gemini-2.0-flash"},
    "openrouter": {"base_url": "https://openrouter.ai/api/v1",                            "model": "openai/gpt-4o-mini"},
    "litellm":    {"base_url": "http://localhost:4000/v1",                                "model": "gpt-4o-mini"},
    "ollama":     {"base_url": "http://localhost:11434/v1",                               "model": "llama3"},
    "claude":     {"base_url": None,                                                       "model": "claude-3-5-haiku-20241022"},
}

PROVIDER_NAMES = {
    "openai":     "OpenAI",
    "deepseek":   "DeepSeek",
    "qwen":       "通义千问 (Qwen)",
    "kimi":       "Kimi (Moonshot)",
    "glm":        "智谱 GLM",
    "minimax":    "MiniMax",
    "gemini":     "Google Gemini",
    "openrouter": "OpenRouter",
    "litellm":    "LiteLLM (本地代理)",
    "ollama":     "Ollama (本地)",
    "claude":     "Anthropic Claude",
}


async def chat_complete(
    provider: str,
    api_key: str,
    base_url: Optional[str],
    model: str,
    messages: list,
    proxy_url: Optional[str] = None,
) -> str:
    try:
        if provider == "claude":
            return await _claude_complete(api_key, model, messages, proxy_url)
        return await _openai_compat_complete(api_key, base_url, model, messages, proxy_url)
    except ImportError as exc:
        logger.error("AI provider dependency missing: provider={} dependency={}", provider, exc.name)
        raise AIProviderError(
            "dependency_missing", "服务器缺少所选 AI 服务所需的依赖"
        ) from None
    except Exception as exc:
        error = _classify_provider_error(exc)
        logger.warning(
            "AI provider request failed: provider={} model={} code={} exception_type={}",
            provider,
            model,
            error.code,
            type(exc).__name__,
        )
        raise error from None


async def test_connection(ai_config: dict, proxy_url: Optional[str] = None) -> str:
    provider = ai_config.get("provider", "deepseek")
    defaults = PROVIDER_DEFAULTS.get(provider, {})
    result = await chat_complete(
        provider=provider,
        api_key=ai_config.get("api_key", ""),
        base_url=ai_config.get("base_url") or defaults.get("base_url"),
        model=ai_config.get("model_name") or defaults.get("model", "gpt-4o-mini"),
        messages=[{"role": "user", "content": "Reply with the number 1."}],
        proxy_url=proxy_url,
    )
    return result.strip()


async def _openai_compat_complete(
    api_key: str,
    base_url: str,
    model: str,
    messages: list,
    proxy_url: Optional[str],
) -> str:
    import httpx
    from openai import AsyncOpenAI

    client_options = {
        "timeout": AI_REQUEST_TIMEOUT_SECONDS,
        "follow_redirects": False,
    }
    if proxy_url:
        try:
            http_client = httpx.AsyncClient(proxy=proxy_url, **client_options)
        except TypeError:
            http_client = httpx.AsyncClient(
                proxies={"all://": proxy_url}, **client_options
            )
    else:
        http_client = httpx.AsyncClient(**client_options)

    client = None
    try:
        client = AsyncOpenAI(
            api_key=api_key or "none",
            base_url=base_url,
            http_client=http_client,
            timeout=AI_REQUEST_TIMEOUT_SECONDS,
        )
        resp = await client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0.1,
            max_tokens=2048,
        )
        return resp.choices[0].message.content
    finally:
        if client is not None:
            await client.close()
        if not http_client.is_closed:
            await http_client.aclose()


async def _claude_complete(
    api_key: str,
    model: str,
    messages: list,
    proxy_url: Optional[str],
) -> str:
    import anthropic
    import httpx

    client_options = {
        "timeout": AI_REQUEST_TIMEOUT_SECONDS,
        "follow_redirects": False,
    }
    if proxy_url:
        try:
            http_client = httpx.AsyncClient(proxy=proxy_url, **client_options)
        except TypeError:
            http_client = httpx.AsyncClient(
                proxies={"all://": proxy_url}, **client_options
            )
    else:
        http_client = httpx.AsyncClient(**client_options)

    client = None
    system = ""
    user_msgs = []
    for m in messages:
        if m["role"] == "system":
            system = m["content"]
        else:
            user_msgs.append(m)

    try:
        client = anthropic.AsyncAnthropic(
            api_key=api_key,
            timeout=AI_REQUEST_TIMEOUT_SECONDS,
            http_client=http_client,
        )
        resp = await client.messages.create(
            model=model,
            max_tokens=2048,
            system=system,
            messages=user_msgs,
            temperature=0,
        )
        return resp.content[0].text
    finally:
        if client is not None:
            await client.close()
        if not http_client.is_closed:
            await http_client.aclose()
