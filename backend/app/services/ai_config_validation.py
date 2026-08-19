"""Validation and SSRF controls for user-supplied AI provider settings."""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from .ai_provider import PROVIDER_DEFAULTS


LOCAL_PROVIDERS = {"custom", "litellm", "ollama"}
PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
)


class AIConfigValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ValidatedAIConfig:
    name: str
    provider: str
    api_key: str
    base_url: str | None
    model_name: str

    def as_provider_config(self) -> dict[str, str | None]:
        return {
            "provider": self.provider,
            "api_key": self.api_key,
            "base_url": self.base_url,
            "model_name": self.model_name,
        }


def _parse_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def _is_allowed_local_ip(value: str) -> bool:
    address = _parse_ip(value)
    if address is None:
        return False
    return address.is_loopback or any(address in network for network in PRIVATE_NETWORKS)


def _is_forbidden_special_ip(value: str) -> bool:
    address = _parse_ip(value)
    if address is None:
        return False
    if address.is_link_local or address.is_multicast or address.is_unspecified or address.is_reserved:
        return True
    return not address.is_global and not _is_allowed_local_ip(value)


def _resolved_ips(host: str) -> set[str]:
    try:
        records = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return set()
    return {record[4][0] for record in records}


def validate_base_url(provider: str, value: str | None) -> str | None:
    default_url = PROVIDER_DEFAULTS[provider]["base_url"]
    raw = (value or default_url or "").strip()
    if provider == "claude":
        if raw:
            raise AIConfigValidationError("Claude 当前不支持自定义 Base URL")
        return None
    if not raw:
        raise AIConfigValidationError("必须填写 Base URL")

    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"}:
        raise AIConfigValidationError("Base URL 只能使用 HTTP 或 HTTPS")
    if not parsed.hostname or parsed.username or parsed.password:
        raise AIConfigValidationError("Base URL 主机无效或包含不允许的登录凭据")
    if parsed.query or parsed.fragment:
        raise AIConfigValidationError("Base URL 不能包含查询参数或 fragment")
    try:
        parsed.port
    except ValueError as exc:
        raise AIConfigValidationError("Base URL 端口无效") from exc

    host = parsed.hostname.lower().rstrip(".")
    if provider in LOCAL_PROVIDERS:
        resolved_ips = _resolved_ips(host)
        if _is_forbidden_special_ip(host) or any(
            _is_forbidden_special_ip(address) for address in resolved_ips
        ):
            raise AIConfigValidationError("Base URL 不能指向链路本地、保留或未指定地址")
    else:
        official_host = urlsplit(default_url).hostname.lower()
        if host != official_host:
            raise AIConfigValidationError("云端 Provider 必须使用官方 API 主机")
        if parsed.scheme != "https":
            raise AIConfigValidationError("云端 Provider 的 Base URL 必须使用 HTTPS")
        if parsed.port not in {None, 443}:
            raise AIConfigValidationError("云端 Provider 的 Base URL 只能使用 HTTPS 标准端口")

    # Strip trailing slash to make persisted values and comparisons deterministic.
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def validate_ai_config(
    *,
    name: str,
    provider: str,
    api_key: str | None,
    base_url: str | None,
    model_name: str | None,
    existing_api_key: str | None = None,
) -> ValidatedAIConfig:
    clean_name = name.strip()
    if not clean_name or len(clean_name) > 50:
        raise AIConfigValidationError("配置名称长度必须为 1 到 50 个字符")

    clean_provider = provider.strip().lower()
    if clean_provider not in PROVIDER_DEFAULTS:
        raise AIConfigValidationError("不支持该 AI Provider")

    clean_model = (model_name or PROVIDER_DEFAULTS[clean_provider]["model"] or "").strip()
    if not clean_model or len(clean_model) > 100 or any(ord(char) < 32 for char in clean_model):
        raise AIConfigValidationError("模型名称无效")

    supplied_key = (api_key or "").strip()
    if supplied_key == "_keep_":
        supplied_key = ""
    effective_key = supplied_key or (existing_api_key or "")
    if clean_provider not in LOCAL_PROVIDERS and not effective_key:
        raise AIConfigValidationError("该 Provider 必须填写 API Key")

    return ValidatedAIConfig(
        name=clean_name,
        provider=clean_provider,
        api_key=effective_key,
        base_url=validate_base_url(clean_provider, base_url),
        model_name=clean_model,
    )
