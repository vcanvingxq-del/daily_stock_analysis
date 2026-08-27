# -*- coding: utf-8 -*-
"""Private portfolio-aware wrapper for the normal daily analysis CLI.

The upstream project already accepts ``portfolio_context`` on API-driven
position analysis, but the scheduled ``main.py`` path analyzes a plain stock
list.  This wrapper keeps the upstream code untouched and appends a private,
per-symbol holding context to the LLM prompt when ``PORTFOLIO_POSITIONS`` is
provided by the runtime environment.

Important privacy property: the raw environment value is never printed.  The
portfolio section is appended at the end of the prompt so the normal INFO-level
500-character prompt preview does not expose holding details.
"""

from __future__ import annotations

import json
import math
import os
import runpy
import sys
from typing import Any, Dict, Mapping, Optional

import src.analyzer as analyzer_module


PORTFOLIO_ENV = "PORTFOLIO_POSITIONS"


def _safe_float(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _normalize_code(value: Any) -> str:
    raw = str(value or "").strip().upper()
    if not raw:
        return ""
    for suffix in (".SH", ".SZ", ".SS", ".BJ"):
        if raw.endswith(suffix):
            raw = raw[: -len(suffix)]
            break
    for prefix in ("SH", "SZ", "SS", "BJ"):
        if raw.startswith(prefix) and raw[len(prefix) :].isdigit():
            raw = raw[len(prefix) :]
            break
    return raw


def _load_private_portfolio() -> Dict[str, Any]:
    raw = (os.getenv(PORTFOLIO_ENV) or "").strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        print(
            f"WARNING: {PORTFOLIO_ENV} is not valid JSON; portfolio personalization is disabled.",
            file=sys.stderr,
        )
        return {}
    if not isinstance(payload, dict):
        print(
            f"WARNING: {PORTFOLIO_ENV} must be a JSON object; portfolio personalization is disabled.",
            file=sys.stderr,
        )
        return {}
    return payload


_PRIVATE_PORTFOLIO = _load_private_portfolio()


def _find_position(code: str) -> Optional[Mapping[str, Any]]:
    target = _normalize_code(code)
    if not target:
        return None
    direct = _PRIVATE_PORTFOLIO.get(target)
    if isinstance(direct, dict):
        return direct
    for key, value in _PRIVATE_PORTFOLIO.items():
        if str(key).startswith("_") or not isinstance(value, dict):
            continue
        if _normalize_code(key) == target:
            return value
    return None


def _current_price(context: Mapping[str, Any]) -> Optional[float]:
    realtime = context.get("realtime")
    if isinstance(realtime, Mapping):
        price = _safe_float(realtime.get("price"))
        if price is not None and price > 0:
            return price
    today = context.get("today")
    if isinstance(today, Mapping):
        price = _safe_float(today.get("close"))
        if price is not None and price > 0:
            return price
    return None


def _portfolio_prompt_section(context: Mapping[str, Any], stock_name: str) -> str:
    code = _normalize_code(context.get("code"))
    position = _find_position(code)
    if not isinstance(position, Mapping):
        return ""

    shares = _safe_float(position.get("shares", position.get("quantity")))
    avg_cost = _safe_float(position.get("cost", position.get("avg_cost")))
    if shares is None or shares <= 0:
        return ""

    current_price = _current_price(context)
    market_value = current_price * shares if current_price is not None else None
    pnl_pct = None
    pnl_amount = None
    if current_price is not None and avg_cost is not None and avg_cost > 0:
        pnl_pct = (current_price / avg_cost - 1.0) * 100.0
        pnl_amount = (current_price - avg_cost) * shares

    account = _PRIVATE_PORTFOLIO.get("_account")
    account = account if isinstance(account, Mapping) else {}
    meta = _PRIVATE_PORTFOLIO.get("_meta")
    meta = meta if isinstance(meta, Mapping) else {}

    lines = [
        "## 🔒 当前持仓上下文（私有输入，仅用于个性化决策）",
        f"- 标的：{stock_name}（{code}）",
        f"- 当前持有：{shares:g} 股",
    ]
    if avg_cost is not None and avg_cost > 0:
        lines.append(f"- 持仓成本：{avg_cost:.4f} 元/股")
    if current_price is not None:
        lines.append(f"- 本轮行情价：{current_price:.2f} 元")
    if market_value is not None:
        lines.append(f"- 按本轮行情估算市值：{market_value:.2f} 元")
    if pnl_pct is not None and pnl_amount is not None:
        lines.append(f"- 按本轮行情估算浮动盈亏：{pnl_amount:+.2f} 元（{pnl_pct:+.2f}%）")

    total_assets = _safe_float(account.get("total_assets"))
    cash = _safe_float(account.get("cash"))
    snapshot_as_of = str(meta.get("as_of") or "").strip()
    if total_assets is not None and total_assets > 0:
        lines.append(f"- 账户总资产快照：{total_assets:.2f} 元" + (f"（截至 {snapshot_as_of}）" if snapshot_as_of else ""))
        if market_value is not None:
            lines.append(f"- 该股占账户总资产约：{market_value / total_assets * 100.0:.2f}%（按快照口径估算）")
    if cash is not None and cash >= 0:
        lines.append(f"- 可用现金快照：{cash:.2f} 元" + (f"（截至 {snapshot_as_of}）" if snapshot_as_of else ""))

    lines.extend(
        [
            "",
            "### 持仓决策约束（必须遵守）",
            "- 先给出独立的个股趋势判断，再给出针对当前持仓的动作；两者不得混为一谈。",
            "- 成本价不是技术支撑位，浮亏本身也不是补仓理由；禁止仅因低于成本价就建议补仓或死扛回本。",
            "- 若趋势/事件证据转弱，优先给出风险处置、反弹减仓或止损计划；若趋势转强，也要结合当前位置与仓位控制追高风险。",
            "- 账户/成本数据来自用户私有快照，若与本轮实时行情冲突，以本轮行情为价格事实，并明确快照可能滞后。",
            "- 不新增 JSON 键；请在现有 operation_advice、dashboard.core_conclusion、dashboard.position_advice、dashboard.battle_plan 等字段中体现持仓动作。",
            "- 最终结论必须能回答：对这笔已经持有的仓位，下一交易日具体是持有、减仓、止损、等待确认还是逢低加仓；给出触发条件和价格纪律。",
        ]
    )
    return "\n".join(lines)


_ORIGINAL_FORMAT_PROMPT = analyzer_module.GeminiAnalyzer._format_prompt


def _portfolio_aware_format_prompt(
    self: Any,
    context: Dict[str, Any],
    name: str,
    news_context: Optional[str] = None,
    report_language: str = "zh",
    analysis_context_pack_summary: Optional[str] = None,
) -> str:
    prompt = _ORIGINAL_FORMAT_PROMPT(
        self,
        context,
        name,
        news_context,
        report_language=report_language,
        analysis_context_pack_summary=analysis_context_pack_summary,
    )
    section = _portfolio_prompt_section(context, context.get("stock_name") or name)
    if not section:
        return prompt
    return f"{prompt}\n\n{section}\n"


analyzer_module.GeminiAnalyzer._format_prompt = _portfolio_aware_format_prompt

# The repository itself warns that legacy deepseek-chat is deprecated.  When a
# DeepSeek key is present, prefer the successor unless the user explicitly set
# LITELLM_MODEL in repository variables/secrets.
if os.getenv("DEEPSEEK_API_KEY") and not (os.getenv("LITELLM_MODEL") or "").strip():
    os.environ["LITELLM_MODEL"] = "deepseek/deepseek-v4-flash"


if __name__ == "__main__":
    runpy.run_module("main", run_name="__main__")

# One-shot GitHub Actions trigger marker. Safe to remove after verification.
