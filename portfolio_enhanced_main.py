# -*- coding: utf-8 -*-
"""Private portfolio-aware wrapper for the normal daily analysis CLI.

The upstream project already accepts ``portfolio_context`` on API-driven
position analysis, but the scheduled ``main.py`` path analyzes a plain stock
list. This wrapper keeps the upstream code untouched and appends a private,
per-symbol holding context to the LLM prompt when ``PORTFOLIO_POSITIONS`` is
provided by the runtime environment.

Privacy notes:
- the raw ``PORTFOLIO_POSITIONS`` environment value is never printed;
- the private section is appended after the normal INFO-level prompt preview;
- analyzer DEBUG logging is raised to INFO so full prompts/responses containing
  holding details are not written by this wrapper.

News-quality notes:
- A-share decision-critical news must contain direct company identity evidence;
- Tavily/news false positives are discarded instead of being treated as company news;
- latest/risk/announcement dimensions are also direct-company-only for A-shares;
- an empty high-quality result set is treated as missing evidence, never as proof
  that no bad news exists.
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
import src.search_service as search_service_module


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
        lines.append(
            f"- 账户总资产快照：{total_assets:.2f} 元"
            + (f"（截至 {snapshot_as_of}）" if snapshot_as_of else "")
        )
    if weight_pct is not None:
        lines.append(f"- 当前该股占账户总资产约：{weight_pct:.2f}%（按本轮行情与账户快照估算）")
    if cash is not None and cash >= 0:
        lines.append(
            f"- 可用现金快照：{cash:.2f} 元"
            + (f"（截至 {snapshot_as_of}）" if snapshot_as_of else "")
        )

    lines.extend(
        [
            "",
            "### 持仓决策约束（必须遵守）",
            "- 先给出独立的个股趋势判断，再给出针对当前持仓的动作；两者不得混为一谈。",
            "- 成本价不是技术支撑位，浮亏本身也不是补仓理由；禁止仅因低于成本价就建议补仓或死扛回本。",
            "- 若趋势/事件证据转弱，优先给出风险处置、反弹减仓或止损计划；若趋势转强，也要结合当前位置与仓位控制追高风险。",
            "- 账户/成本数据来自用户私有快照，若与本轮实时行情冲突，以本轮行情为价格事实，并明确快照可能滞后。",
            "- 不新增 JSON 键；请在现有 operation_advice、dashboard.core_conclusion、dashboard.position_advice、dashboard.battle_plan 等字段中体现持仓动作。",
            "- dashboard.position_advice.holding 必须明确引用当前持股数量、成本、估算盈亏，并结合账户仓位占比给出动作；不得只写泛化的‘持有观察’。",
            "- 若上方给出了当前账户占比，必须把它当作当前仓位事实；禁止把约30%误写成1成等明显冲突的仓位。",
            "- 必须区分‘当前仓位占比’和‘建议目标仓位’，如建议减仓，要明确从当前占比向什么目标区间调整，不能混写。",
            "- 最终结论必须能回答：对这笔已经持有的仓位，下一交易日具体是持有、减仓、止损、等待确认还是逢低加仓；给出触发条件和价格纪律。",
        ]
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Portfolio-specific news quality layer
# ---------------------------------------------------------------------------

_ORIGINAL_SEARCH_STOCK_NEWS = search_service_module.SearchService.search_stock_news
_ORIGINAL_SEARCH_COMPREHENSIVE_INTEL = (
    search_service_module.SearchService.search_comprehensive_intel
)


def _is_a_share_target(stock_code: str, stock_name: str) -> bool:
    code = _normalize_code(stock_code)
    return code.isdigit() and len(code) == 6 and bool((stock_name or "").strip())


def _a_share_news_focus(stock_code: str, stock_name: str) -> list[str]:
    """High-precision first-pass query for decision-useful A-share events."""
    code = _normalize_code(stock_code)
    return [
        f'"{stock_name.strip()}"',
        code,
        "最新 公告 业绩 订单 中标 重大合同 合作 减持 增持 处罚 诉讼 监管",
    ]


def _a_share_news_fallback_focus(stock_code: str, stock_name: str) -> list[str]:
    """Broader fallback that still anchors both company name and stock code."""
    code = _normalize_code(stock_code)
    return [f'"{stock_name.strip()}"', code, "最新消息 公告"]


def _direct_only_response(response: Any, *, log_scope: str) -> Any:
    """Keep only results whose scorer found direct target-company identity."""
    if not response.success or not response.results:
        return response

    direct_category = search_service_module.SearchService._DIRECT_NEWS_CATEGORY
    direct_results = [
        item for item in response.results if item.relevance_category == direct_category
    ]
    if len(direct_results) != len(response.results):
        search_service_module.logger.info(
            "[持仓新闻增强] %s: provider=%s, total=%s, direct=%s, 丢弃非目标公司结果=%s",
            log_scope,
            response.provider,
            len(response.results),
            len(direct_results),
            len(response.results) - len(direct_results),
        )

    return search_service_module.SearchResponse(
        query=response.query,
        results=direct_results,
        provider=response.provider if direct_results else "Filtered",
        success=response.success,
        error_message=response.error_message,
        search_time=response.search_time,
    )


def _portfolio_search_stock_news(
    self: Any,
    stock_code: str,
    stock_name: str,
    max_results: int = 5,
    focus_keywords: Optional[list[str]] = None,
) -> Any:
    """For A-shares, never pass non-company Tavily/news matches to the LLM."""
    if focus_keywords or not _is_a_share_target(stock_code, stock_name):
        return _ORIGINAL_SEARCH_STOCK_NEWS(
            self,
            stock_code,
            stock_name,
            max_results=max_results,
            focus_keywords=focus_keywords,
        )

    primary = _ORIGINAL_SEARCH_STOCK_NEWS(
        self,
        stock_code,
        stock_name,
        max_results=max_results,
        focus_keywords=_a_share_news_focus(stock_code, stock_name),
    )
    primary_direct = _direct_only_response(
        primary,
        log_scope=f"{stock_code}:primary",
    )
    if primary_direct.success and primary_direct.results:
        return primary_direct
    if not primary.success:
        return primary

    search_service_module.logger.info(
        "[持仓新闻增强] %s(%s) 首轮没有直接个股新闻，执行精确身份回退查询",
        stock_name,
        stock_code,
    )
    fallback = _ORIGINAL_SEARCH_STOCK_NEWS(
        self,
        stock_code,
        stock_name,
        max_results=max_results,
        focus_keywords=_a_share_news_fallback_focus(stock_code, stock_name),
    )
    fallback_direct = _direct_only_response(
        fallback,
        log_scope=f"{stock_code}:fallback",
    )
    if fallback_direct.success and fallback_direct.results:
        return fallback_direct

    # At least one provider call succeeded, but no result passed direct-company
    # identity checks. This is a valid empty evidence state, not a search error.
    if primary.success or fallback.success:
        return search_service_module.SearchResponse(
            query=fallback.query or primary.query,
            results=[],
            provider="Filtered",
            success=True,
            error_message=None,
            search_time=(primary.search_time or 0) + (fallback.search_time or 0),
        )
    return fallback


def _portfolio_search_comprehensive_intel(
    self: Any,
    stock_code: str,
    stock_name: str,
    max_searches: int = 3,
) -> Dict[str, Any]:
    """Post-filter decision-critical A-share intel dimensions to direct identity.

    Broader market/earnings/industry dimensions are left untouched because they
    are explicitly contextual research. Current-news, risk and announcement
    dimensions must be about the target company itself.
    """
    results = _ORIGINAL_SEARCH_COMPREHENSIVE_INTEL(
        self,
        stock_code,
        stock_name,
        max_searches=max_searches,
    )
    if not _is_a_share_target(stock_code, stock_name):
        return results

    for dim in ("latest_news", "risk_check", "announcements"):
        response = results.get(dim)
        if response is not None:
            results[dim] = _direct_only_response(
                response,
                log_scope=f"{stock_code}:intel:{dim}",
            )
    return results


search_service_module.SearchService.search_stock_news = _portfolio_search_stock_news
search_service_module.SearchService.search_comprehensive_intel = (
    _portfolio_search_comprehensive_intel
)


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

    # For portfolio decisions, no evidence must never be converted into a
    # factual claim that there is no bad news.
    if not news_context:
        if not _news_search_configured():
            prompt = prompt.replace(
                "未搜索到该股票近期的相关新闻。请主要依据技术面数据进行分析。",
                "本轮未配置可用的新闻搜索能力，因此没有执行新闻检索。请主要依据技术面数据进行分析。",
            )
            evidence_state = (
                "本轮新闻检索未执行，必须写‘新闻/消息面数据缺失’或‘未配置新闻搜索能力’。"
            )
        else:
            prompt = prompt.replace(
                "未搜索到该股票近期的相关新闻。请主要依据技术面数据进行分析。",
                "本轮已配置新闻搜索，但经过时效与目标公司身份过滤后，没有获得足够可靠的直接个股新闻。请主要依据技术面数据进行分析。",
            )
            evidence_state = (
                "本轮没有获得足够高相关度的直接个股新闻，只能写‘未取得可靠消息面证据’，不能等同于‘没有利空’。"
            )

        prompt += (
            "\n\n### 新闻证据约束（强制）\n"
            f"- {evidence_state}\n"
            "- 禁止写‘近期无重大利空’‘未搜索到重大利空’‘消息面暂无利空’等无证据结论。\n"
            "- `latest_news`、`risk_alerts`、`positive_catalysts` 不得把‘缺少搜索证据’包装成事实性新闻判断。\n"
        )

    section = _portfolio_prompt_section(context, context.get("stock_name") or name)
    if not section:
        return prompt
    return f"{prompt}\n\n{section}\n"


def _stable_thinking_extra_body(model: str) -> Optional[dict]:
    """Use V4 Flash in non-thinking mode for deterministic JSON reports."""
    normalized = (model or "").strip().lower()
    if normalized == "deepseek-v4-flash" or normalized.startswith("deepseek-v4-flash-"):
        return {"thinking": {"type": "disabled"}}
    return _ORIGINAL_THINKING_EXTRA_BODY(model)


analyzer_module.GeminiAnalyzer._format_prompt = _portfolio_aware_format_prompt
analyzer_module.get_thinking_extra_body = _stable_thinking_extra_body

# The repository itself warns that legacy deepseek-chat is deprecated. When a
# DeepSeek key is present, prefer the successor unless the user explicitly set
# LITELLM_MODEL in repository variables/secrets.
if os.getenv("DEEPSEEK_API_KEY") and not (os.getenv("LITELLM_MODEL") or "").strip():
    os.environ["LITELLM_MODEL"] = "deepseek/deepseek-v4-flash"


if __name__ == "__main__":
    runpy.run_module("main", run_name="__main__")
