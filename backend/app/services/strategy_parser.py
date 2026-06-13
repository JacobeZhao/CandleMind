"""
Parses natural language strategy descriptions into structured JSON via LLM.
Returns a dict with {strategy, indicators_needed, trading, confidence, explanation}.
"""
import json
import re

from .ai_provider import chat_complete, PROVIDER_DEFAULTS
from .indicators import REGISTRY

_SYSTEM = """\
你是一个专业的量化交易策略解析器。将用户的自然语言描述转换成结构化 JSON 配置。
只返回合法 JSON，不要包含任何其他文字、代码块标记或解释。

可用指标（indicator_id → 说明 → 默认参数）：
{indicator_list}

条件类型说明：
- cross_above: 指定列从下方穿越另一列或数值
- cross_below: 指定列从上方穿越另一列或数值
- above: 当前值大于 value 或 vs_column
- below: 当前值小于 value 或 vs_column
- rising: 当前值 > 前一个值
- falling: 当前值 < 前一个值
- equals: 值等于 value（用于整数列如 supertrend_dir）

返回 JSON 格式（严格遵守，不得添加注释）：
{{
  "strategy": {{
    "entry_long":  [{{ "indicator": "xxx", "column": "yyy", "condition": "cross_above", "vs_column": "zzz" }}],
    "entry_short": [...],
    "exit_long":   [...],
    "exit_short":  [...]
  }},
  "indicators_needed": [
    {{ "id": "macd",  "params": {{ "fast": 12, "slow": 26, "signal": 9 }} }},
    {{ "id": "rsi",   "params": {{ "period": 14 }} }}
  ],
  "trading": {{
    "symbol": "BTCUSDT",
    "interval": "15m",
    "leverage": 5,
    "risk_pct": 0.01,
    "stop_loss_pct": 0.015,
    "take_profit_pct": 0.03
  }},
  "confidence": 0.9,
  "explanation": "中文说明，解释策略逻辑和入场出场条件"
}}

示例1——MACD金叉做多：
用户输入"MACD金叉开多，死叉开空，BTCUSDT 1小时线 5倍杠杆"
→ entry_long:  [{{ "indicator": "macd", "column": "macd_line", "condition": "cross_above", "vs_column": "macd_signal" }}]
→ entry_short: [{{ "indicator": "macd", "column": "macd_line", "condition": "cross_below", "vs_column": "macd_signal" }}]

示例2——RSI超卖做多：
用户输入"RSI低于30时做多，高于70时做空"
→ entry_long:  [{{ "indicator": "rsi14", "column": "rsi14", "condition": "cross_above", "value": 30 }}]
→ entry_short: [{{ "indicator": "rsi",   "column": "rsi14", "condition": "cross_below", "value": 70 }}]

示例3——超级趋势+RSI组合：
→ entry_long: [
    {{ "indicator": "supertrend", "column": "supertrend_dir", "condition": "equals", "value": 1 }},
    {{ "indicator": "rsi", "column": "rsi14", "condition": "below", "value": 60 }}
  ]
"""


def _indicator_list_str() -> str:
    lines = []
    for iid, meta in REGISTRY.items():
        lines.append(f"  {iid}: {meta['name']} ({meta['category']}) 默认参数={meta['params']}")
    return "\n".join(lines)


async def parse_strategy(text: str, ai_config: dict, proxy_url: str = None) -> dict:
    """
    ai_config: {provider, api_key, base_url, model_name}
    Returns parsed strategy dict.
    """
    provider = ai_config.get("provider", "deepseek")
    defaults = PROVIDER_DEFAULTS.get(provider, {})
    base_url = ai_config.get("base_url") or defaults.get("base_url")
    model    = ai_config.get("model_name") or defaults.get("model", "gpt-4o-mini")

    system = _SYSTEM.format(indicator_list=_indicator_list_str())
    messages = [
        {"role": "system", "content": system},
        {"role": "user",   "content": f"请解析以下交易策略：\n{text}"},
    ]

    raw = await chat_complete(
        provider=provider,
        api_key=ai_config.get("api_key", ""),
        base_url=base_url,
        model=model,
        messages=messages,
        proxy_url=proxy_url,
    )

    # Extract JSON block (LLM may wrap in ```json ... ```)
    match = re.search(r"\{[\s\S]*\}", raw)
    if not match:
        raise ValueError(f"AI 未返回有效 JSON，原始输出：{raw[:300]}")

    try:
        return json.loads(match.group())
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON 解析失败: {e}\n原始输出：{raw[:300]}")


async def test_connection(ai_config: dict, proxy_url: str = None) -> str:
    """Send a minimal request to verify the AI config works."""
    provider = ai_config.get("provider", "deepseek")
    defaults = PROVIDER_DEFAULTS.get(provider, {})
    base_url = ai_config.get("base_url") or defaults.get("base_url")
    model    = ai_config.get("model_name") or defaults.get("model", "gpt-4o-mini")

    messages = [{"role": "user", "content": "回复数字 1"}]
    result = await chat_complete(
        provider=provider,
        api_key=ai_config.get("api_key", ""),
        base_url=base_url,
        model=model,
        messages=messages,
        proxy_url=proxy_url,
    )
    return result.strip()
