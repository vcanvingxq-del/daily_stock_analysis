# -*- coding: utf-8 -*-
"""Private portfolio-aware wrapper for the normal daily analysis CLI.

The upstream project already accepts ``portfolio_context`` on API-driven
position analysis, but the scheduled ``main.py`` path analyzes a plain stock
list.  This wrapper keeps the upstream code untouched and appends a private,
per-symbol holding context to the LLM prompt when ``PORTFOLIO_POSITIONS`` is
provided by the runtime environment.

Privacy notes:
- the raw ``PORTFOLIO_POSITIONS`` environment value is never printed;
- the private section is appended after the normal INFO-level prompt preview;
- analyzer DEBUG logging is raised to INFO so full prompts/responses containing
  holding details are not written by this wrapper.
"""

from __future__ import annotations

import json
import logging
import math
import os
import runpy
import sys
from typing import Any, Dict, Mapping, Optional

import src.analyzer as analyzer_module


PORTFOLIO_ENV = "PORTFOLIO_POSITIONS"
NEWS_SEARCH_ENV_KEYS = (
    "BOCHA_API_KEYS",
    "TAVILY_API_KEYS",
    "SERPAPI_API_KEYS",
    "BRAVE_API_KEYS",
    "MINIMAX_API_KEYS",
    "ANSPIRE_API_KEYS",
    "SEARXNG_BASE_URLS",
)

# Full analyzer DEBUG prompt/response dumps can contain private holding details.
# Keep ordinary INFO diagnostics while suppressing those full payload dumps.
analyzer_module.logger.setLevel(logging.INFO)


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


def _news_search_configured() -> bool:
    return any((os.getenv(key) or "").strip() for key in NEWS_SEARCH_ENV_KEYS)


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

    total_assets = _safe_float(account.get("total_assets"))
    cash = _safe_float(account.get("cash"))
    weight_pct = None
    if total_assets is not None and total_assets > 0 and market_value is not None:
        weight_pct = market_value / total_assets * 100.0

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

    snapshot_as_of = str(meta.get("as_of") or "").strip()
    if total_assets is not None and total_assets > 0:
        lines.append(f"- 账户总资产快照：{total_assets:.2f} 元" + (f"（截至 {snapshot_as_of}）" if snapshot_as_of else ""))
    if weight_pct is not None:
        lines.append(f"- 当前该股占账户总资产约：{weight_pct:.2f}%（按本轮行情与账户快照估算）")
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
            "- dashboard.position_advice.holding 必须明确引用当前持股数量、成本、估算盈亏，并结合账户仓位占比给出动作；不得只写泛化的“持有观察”。",
            "- 若上方给出了当前账户占比，必须把它当作当前仓位事实；禁止把约30%误写成1成等明显冲突的仓位。",
            "- 必须区分“当前仓位占比”和“建议目标仓位”，如建议减仓，要明确从当前占比向什么目标区间调整，不能混写。",
            "- 最终结论必须能回答：对这笔已经持有的仓位，下一交易日具体是持有、减仓、止损、等待确认还是逢低加仓；给出触发条件和价格纪律。",
        ]
    )
    return "\n".join(lines)


_ORIGINAL_FORMAT_PROMPT = analyzer_module.GeminiAnalyzer._format_prompt
_ORIGINAL_THINKING_EXTRA_BODY = analyzer_module.get_thinking_extra_body


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

    # Upstream currently uses the same wording for “search not configured” and
    # “search executed but returned zero hits”.  For portfolio decisions that is
    # too strong: no search evidence must never be reported as “no bad news”.
    if not news_context and not _news_search_configured():
        prompt = prompt.replace(
            "未搜索到该股票近期的相关新闻。请主要依据技术面数据进行分析。",
            "本轮未配置可用的新闻搜索能力，因此没有执行新闻检索。请主要依据技术面数据进行分析。",
        )
        prompt += (
            "\n\n### 新闻证据约束（强制）\n"
            "- 本轮新闻检索未执行，必须写“新闻/消息面数据缺失”或“未配置新闻搜索能力”。\n"
            "- 禁止写“近期无重大利空”“未搜索到重大利空”“消息面暂无利空”等结论。\n"
            "- `latest_news`、`risk_alerts`、`positive_catalysts` 不得把“没有搜索证据”包装成事实性新闻判断。\n"
        )

    section = _portfolio_prompt_section(context, context.get("stock_name") or name)
    if not section:
        return prompt
    return f"{prompt}\n\n{section}\n"


def _stable_thinking_extra_body(model: str) -> Optional[dict]:
    """Use V4 Flash in non-thinking mode for deterministic JSON reports.

    DeepSeek V4 Flash defaults to thinking mode.  The upstream stream parser
    intentionally consumes final ``content`` only, not ``reasoning_content``;
    disabling thinking avoids long reasoning-only streams and intermittent
    empty final-content failures for this structured-report workload.
    """
    normalized = (model or "").strip().lower()
    if normalized == "deepseek-v4-flash" or normalized.startswith("deepseek-v4-flash-"):
        return {"thinking": {"type": "disabled"}}
    return _ORIGINAL_THINKING_EXTRA_BODY(model)


analyzer_module.GeminiAnalyzer._format_prompt = _portfolio_aware_format_prompt
analyzer_module.get_thinking_extra_body = _stable_thinking_extra_body

# The repository itself warns that legacy deepseek-chat is deprecated.  When a
# DeepSeek key is present, prefer the successor unless the user explicitly set
# LITELLM_MODEL in repository variables/secrets.
if os.getenv("DEEPSEEK_API_KEY") and not (os.getenv("LITELLM_MODEL") or "").strip():
    os.environ["LITELLM_MODEL"] = "deepseek/deepseek-v4-flash"


if __name__ == "__main__":
    runpy.run_module("main", run_name="__main__")
