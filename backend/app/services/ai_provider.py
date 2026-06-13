"""
Unified LLM provider interface.
OpenAI-compatible providers (deepseek/qwen/kimi/glm/minimax/gemini/openrouter/litellm/ollama)
all use the openai SDK with a custom base_url.
Claude uses the anthropic SDK separately.
"""
from typing import Optional

PROVIDER_DEFAULTS = {
    "openai":     {"base_url": "https://api.openai.com/v1",                               "model": "gpt-4o-mini"},
    "deepseek":   {"base_url": "https://api.deepseek.com/v1",                             "model": "deepseek-chat"},
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
    if provider == "claude":
        return await _claude_complete(api_key, model, messages)
    return await _openai_compat_complete(api_key, base_url, model, messages, proxy_url)


async def _openai_compat_complete(
    api_key: str,
    base_url: str,
    model: str,
    messages: list,
    proxy_url: Optional[str],
) -> str:
    import httpx
    from openai import AsyncOpenAI

    http_client = None
    if proxy_url:
        try:
            http_client = httpx.AsyncClient(proxy=proxy_url, timeout=30)
        except TypeError:
            http_client = httpx.AsyncClient(proxies={"all://": proxy_url}, timeout=30)

    client = AsyncOpenAI(
        api_key=api_key or "none",
        base_url=base_url,
        http_client=http_client,
        timeout=30,
    )
    resp = await client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0.1,
        max_tokens=2048,
    )
    if http_client:
        await http_client.aclose()
    return resp.choices[0].message.content


async def _claude_complete(api_key: str, model: str, messages: list) -> str:
    import anthropic

    client = anthropic.AsyncAnthropic(api_key=api_key, timeout=30)
    system = ""
    user_msgs = []
    for m in messages:
        if m["role"] == "system":
            system = m["content"]
        else:
            user_msgs.append(m)

    resp = await client.messages.create(
        model=model,
        max_tokens=2048,
        system=system,
        messages=user_msgs,
        temperature=0,
    )
    return resp.content[0].text
