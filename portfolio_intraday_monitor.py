# -*- coding: utf-8 -*-
"""Low-noise intraday portfolio monitor with PushPlus delivery.

The monitor is intentionally separate from the 18:00 deep-analysis workflow:
- polling is lightweight and deterministic (no LLM call on every cycle);
- all active positions are read from the private PORTFOLIO_POSITIONS env JSON;
- high-concentration holdings automatically receive a tighter alert profile;
- only threshold crossings are notified, with at most one notification per
  event/direction per trading day;
- local state is persisted so service restarts do not create alert storms.

Run once:
    python portfolio_intraday_monitor.py --once --dry-run

Run as a daemon (recommended on a small always-on server):
    python portfolio_intraday_monitor.py --daemon --interval 120
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, time as dt_time
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional
from zoneinfo import ZoneInfo


PORTFOLIO_ENV = "PORTFOLIO_POSITIONS"
DEFAULT_STATE_PATH = "data/portfolio_intraday_monitor_state.json"
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")

logger = logging.getLogger("portfolio_intraday_monitor")


def _safe_float(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _normalize_code(value: Any) -> str:
    raw = str(value or "").strip().upper()
    for suffix in (".SH", ".SZ", ".SS", ".BJ"):
        if raw.endswith(suffix):
            raw = raw[: -len(suffix)]
            break
    for prefix in ("SH", "SZ", "SS", "BJ"):
        if raw.startswith(prefix) and raw[len(prefix) :].isdigit():
            raw = raw[len(prefix) :]
            break
    return raw


def _load_portfolio() -> Dict[str, Any]:
    raw = (os.getenv(PORTFOLIO_ENV) or "").strip()
    if not raw:
        raise RuntimeError(f"{PORTFOLIO_ENV} is not configured")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{PORTFOLIO_ENV} is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{PORTFOLIO_ENV} must be a JSON object")
    return payload


def _active_positions(payload: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    positions: Dict[str, Dict[str, Any]] = {}
    for raw_code, value in payload.items():
        if str(raw_code).startswith("_") or not isinstance(value, dict):
            continue
        shares = _safe_float(value.get("shares", value.get("quantity")))
        code = _normalize_code(raw_code)
        if code and shares is not None and shares > 0:
            positions[code] = dict(value)
    return positions


def _quote_float(quote: Any, *names: str) -> Optional[float]:
    for name in names:
        raw = quote.get(name) if isinstance(quote, dict) else getattr(quote, name, None)
        if raw is None and hasattr(quote, "to_dict"):
            try:
                raw = quote.to_dict().get(name)
            except Exception:
                raw = None
        if isinstance(raw, str):
            raw = raw.strip().replace(",", "").rstrip("%").strip()
        value = _safe_float(raw)
        if value is not None:
            return value
    return None


def _quote_text(quote: Any, *names: str) -> str:
    for name in names:
        raw = quote.get(name) if isinstance(quote, dict) else getattr(quote, name, None)
        if raw is None and hasattr(quote, "to_dict"):
            try:
                raw = quote.to_dict().get(name)
            except Exception:
                raw = None
        if raw is not None and str(raw).strip():
            return str(raw).strip()
    return ""


class JsonStateStore:
    def __init__(self, path: str):
        self.path = Path(path)
        self.data: Dict[str, Any] = {"symbols": {}}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            parsed = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                self.data = parsed
        except Exception as exc:
            logger.warning("State file could not be loaded; starting clean: %s", exc)

    def symbol(self, code: str) -> Dict[str, Any]:
        symbols = self.data.setdefault("symbols", {})
        value = symbols.setdefault(code, {})
        return value if isinstance(value, dict) else {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self.data, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        tmp.replace(self.path)


@dataclass(frozen=True)
class PriceLevel:
    key: str
    label: str
    price: float
    direction: str  # above/below
    severity: str = "warning"


class IntradayPortfolioMonitor:
    def __init__(self, *, state_path: str, dry_run: bool = False, verbose: bool = False):
        self.payload = _load_portfolio()
        self.positions = _active_positions(self.payload)
        if not self.positions:
            raise RuntimeError("No active positions found in PORTFOLIO_POSITIONS")

        account = self.payload.get("_account")
        self.account = account if isinstance(account, dict) else {}
        alerts = self.payload.get("_alerts")
        self.alert_overrides = alerts if isinstance(alerts, dict) else {}

        self.total_assets = _safe_float(self.account.get("total_assets"))
        self.state = JsonStateStore(state_path)
        self.dry_run = dry_run
        self.verbose = verbose
        self._fetcher = None
        self._sender = None

    @property
    def fetcher(self):
        if self._fetcher is None:
            from data_provider import DataFetcherManager

            self._fetcher = DataFetcherManager()
        return self._fetcher

    @property
    def sender(self):
        if self._sender is None:
            from src.config import get_config
            from src.notification_sender.pushplus_sender import PushplusSender

            self._sender = PushplusSender(get_config())
        return self._sender

    @staticmethod
    def in_trading_window(now: datetime) -> bool:
        if now.weekday() >= 5:
            return False
        current = now.time()
        morning = dt_time(9, 25) <= current <= dt_time(11, 35)
        afternoon = dt_time(12, 55) <= current <= dt_time(15, 5)
        return morning or afternoon

    def _priority(self, code: str, position: Mapping[str, Any], price: float) -> tuple[str, Optional[float]]:
        shares = _safe_float(position.get("shares", position.get("quantity"))) or 0.0
        override = self.alert_overrides.get(code)
        explicit = str(override.get("priority") or "").upper() if isinstance(override, dict) else ""
        market_value = shares * price if price > 0 else None
        weight = None
        if market_value is not None and self.total_assets and self.total_assets > 0:
            weight = market_value / self.total_assets * 100.0

        if explicit in {"S", "A", "B"}:
            return explicit, weight
        cost = _safe_float(position.get("cost", position.get("avg_cost")))
        pnl_pct = ((price / cost) - 1.0) * 100.0 if cost and cost > 0 and price > 0 else None
        if weight is not None and weight >= 25:
            return "S", weight
        if (weight is not None and weight >= 10) or (pnl_pct is not None and pnl_pct <= -15):
            return "A", weight
        return "B", weight

    def _daily_levels(self, code: str, symbol_state: Dict[str, Any], today: str) -> Dict[str, float]:
        cached = symbol_state.get("daily_levels")
        if symbol_state.get("daily_levels_day") == today and isinstance(cached, dict):
            return {k: float(v) for k, v in cached.items() if _safe_float(v) is not None}

        levels: Dict[str, float] = {}
        try:
            result = self.fetcher.get_daily_data(code, days=45)
            if result is not None:
                df, _source = result
                if df is not None and not df.empty:
                    data = df.copy()
                    if "date" in data.columns:
                        dates = data["date"].astype(str).str.slice(0, 10)
                        completed = data[dates < today]
                        if not completed.empty:
                            data = completed
                    closes = data["close"].astype(float)
                    if len(closes) >= 10:
                        levels["ma10"] = float(closes.tail(10).mean())
                    if len(closes) >= 20:
                        tail20 = data.tail(20)
                        levels["ma20"] = float(closes.tail(20).mean())
                        high_col = "high" if "high" in tail20.columns else "close"
                        low_col = "low" if "low" in tail20.columns else "close"
                        levels["high20"] = float(tail20[high_col].astype(float).max())
                        levels["low20"] = float(tail20[low_col].astype(float).min())
        except Exception as exc:
            logger.debug("Daily levels unavailable: %s", exc)

        symbol_state["daily_levels_day"] = today
        symbol_state["daily_levels"] = levels
        return levels

    def _custom_levels(self, code: str) -> Iterable[PriceLevel]:
        override = self.alert_overrides.get(code)
        if not isinstance(override, dict):
            return []
        raw_levels = override.get("levels")
        if not isinstance(raw_levels, list):
            return []
        output = []
        for idx, item in enumerate(raw_levels):
            if not isinstance(item, dict):
                continue
            price = _safe_float(item.get("price"))
            direction = str(item.get("direction") or "").strip().lower()
            if price is None or price <= 0 or direction not in {"above", "below"}:
                continue
            label = str(item.get("label") or f"自定义关键位{idx + 1}").strip()
            output.append(
                PriceLevel(
                    key=f"custom:{idx}:{direction}:{price:.4f}",
                    label=label,
                    price=price,
                    direction=direction,
                    severity=str(item.get("severity") or "warning"),
                )
            )
        return output

    def _levels_for(
        self,
        code: str,
        position: Mapping[str, Any],
        priority: str,
        technical: Mapping[str, float],
    ) -> list[PriceLevel]:
        levels: list[PriceLevel] = list(self._custom_levels(code))
        cost = _safe_float(position.get("cost", position.get("avg_cost")))
        if cost and cost > 0:
            levels.extend(
                [
                    PriceLevel("cost:above", "重新站上持仓成本", cost, "above", "info"),
                    PriceLevel("cost:below", "跌回持仓成本下方", cost, "below", "warning"),
                ]
            )

        high20 = _safe_float(technical.get("high20"))
        low20 = _safe_float(technical.get("low20"))
        ma20 = _safe_float(technical.get("ma20"))
        ma10 = _safe_float(technical.get("ma10"))
        if high20 and high20 > 0:
            levels.append(PriceLevel("tech:high20", "突破20日高点", high20, "above", "info"))
        if low20 and low20 > 0:
            levels.append(PriceLevel("tech:low20", "跌破20日低点", low20, "below", "critical"))
        if priority in {"S", "A"} and ma20 and ma20 > 0:
            levels.extend(
                [
                    PriceLevel("tech:ma20:above", "重新站上MA20", ma20, "above", "info"),
                    PriceLevel("tech:ma20:below", "跌破MA20", ma20, "below", "warning"),
                ]
            )
        if priority == "S" and ma10 and ma10 > 0:
            levels.extend(
                [
                    PriceLevel("tech:ma10:above", "重新站上MA10", ma10, "above", "info"),
                    PriceLevel("tech:ma10:below", "跌破MA10", ma10, "below", "warning"),
                ]
            )
        return levels

    @staticmethod
    def _crossed(previous: Optional[float], current: float, level: PriceLevel) -> bool:
        if previous is None or previous <= 0 or current <= 0:
            return False
        if level.direction == "above":
            return previous < level.price <= current
        return previous > level.price >= current

    @staticmethod
    def _already_notified_today(symbol_state: Mapping[str, Any], event_key: str, today: str) -> bool:
        events = symbol_state.get("notified_events")
        return isinstance(events, dict) and events.get(event_key) == today

    @staticmethod
    def _mark_notified(symbol_state: Dict[str, Any], event_key: str, today: str) -> None:
        events = symbol_state.setdefault("notified_events", {})
        events[event_key] = today
        if len(events) > 120:
            symbol_state["notified_events"] = {
                key: value for key, value in events.items() if value == today
            }

    def _suggestion(self, *, priority: str, direction: str, label: str) -> str:
        if direction == "below":
            if priority == "S":
                return "高集中度仓位风险节点已触发：先看能否快速收回，暂不加仓；若继续走弱，优先评估降低单票仓位。"
            return "风险节点已触发：暂不因下跌补仓，先观察能否收回关键位，再决定是否降低风险。"
        if "成本" in label:
            return "已回到成本附近：优先评估是否借修复降低被套仓位，不因回本情绪继续追高。"
        if priority == "S":
            return "高集中度仓位出现向上确认：先看能否站稳，避免追高；若冲高回落，仍以仓位控制为先。"
        return "出现向上确认：先观察站稳情况，不因单次突破追高。"

    def _notify_price_level(
        self,
        *,
        code: str,
        name: str,
        position: Mapping[str, Any],
        price: float,
        change_pct: Optional[float],
        weight: Optional[float],
        priority: str,
        level: PriceLevel,
    ) -> bool:
        shares = _safe_float(position.get("shares", position.get("quantity"))) or 0.0
        cost = _safe_float(position.get("cost", position.get("avg_cost")))
        pnl_pct = ((price / cost) - 1.0) * 100.0 if cost and cost > 0 else None
        icon = {"S": "🔴", "A": "🟠", "B": "🔵"}.get(priority, "🔵")
        verb = "突破" if level.direction == "above" else "跌破"
        title = f"{icon} [{priority}级] {name} {verb}关键位"
        lines = [
            f"### {name}（{code}）",
            f"- **触发**：{level.label} {level.price:.2f} 元",
            f"- **当前价**：{price:.2f} 元",
        ]
        if change_pct is not None:
            lines.append(f"- **当日涨跌**：{change_pct:+.2f}%")
        lines.append(f"- **持仓**：{shares:g} 股")
        if cost and cost > 0:
            lines.append(f"- **成本**：{cost:.4f} 元")
        if pnl_pct is not None:
            lines.append(f"- **估算持仓盈亏**：{pnl_pct:+.2f}%")
        if weight is not None:
            lines.append(f"- **账户占比**：约 {weight:.1f}%")
        if priority == "S":
            lines.append("- **优先级**：S级（高集中度持仓，优先处理）")
        lines.extend(["", f"**处理提示**：{self._suggestion(priority=priority, direction=level.direction, label=level.label)}"])
        return self._send(title, "\n".join(lines))

    def _notify_change(
        self,
        *,
        code: str,
        name: str,
        position: Mapping[str, Any],
        price: float,
        change_pct: float,
        weight: Optional[float],
        priority: str,
        direction: str,
        threshold: float,
    ) -> bool:
        shares = _safe_float(position.get("shares", position.get("quantity"))) or 0.0
        cost = _safe_float(position.get("cost", position.get("avg_cost")))
        pnl_pct = ((price / cost) - 1.0) * 100.0 if cost and cost > 0 else None
        icon = {"S": "🔴", "A": "🟠", "B": "🔵"}.get(priority, "🔵")
        word = "快速上涨" if direction == "up" else "快速下跌"
        title = f"{icon} [{priority}级] {name} {word}提醒"
        lines = [
            f"### {name}（{code}）",
            f"- **触发**：当日{word}达到 {threshold:.1f}% 监控线",
            f"- **当前价**：{price:.2f} 元",
            f"- **当日涨跌**：{change_pct:+.2f}%",
            f"- **持仓**：{shares:g} 股",
        ]
        if cost and cost > 0:
            lines.append(f"- **成本**：{cost:.4f} 元")
        if pnl_pct is not None:
            lines.append(f"- **估算持仓盈亏**：{pnl_pct:+.2f}%")
        if weight is not None:
            lines.append(f"- **账户占比**：约 {weight:.1f}%")
        if priority == "S":
            lines.append("- **优先级**：S级（高集中度持仓，优先处理）")
        suggestion = (
            "高仓位波动明显放大：优先检查关键支撑与消息面，不因急跌补仓。"
            if direction == "down" and priority == "S"
            else "波动明显放大：检查是否接近关键支撑/压力位，避免情绪化追涨杀跌。"
        )
        lines.extend(["", f"**处理提示**：{suggestion}"])
        return self._send(title, "\n".join(lines))

    def _send(self, title: str, content: str) -> bool:
        if self.dry_run:
            if self.verbose:
                print(f"DRY_RUN_NOTIFICATION title={title}")
            return True
        return bool(self.sender.send_to_pushplus(content, title=title, timeout_seconds=8))

    def run_once(self, *, force: bool = False) -> Dict[str, int]:
        now = datetime.now(SHANGHAI_TZ)
        stats = {"positions": len(self.positions), "quotes": 0, "events": 0, "sent": 0, "errors": 0}
        if not force and not self.in_trading_window(now):
            return stats

        today = now.date().isoformat()
        for code, position in self.positions.items():
            symbol_state = self.state.symbol(code)
            try:
                quote = self.fetcher.get_realtime_quote(code)
                if quote is None:
                    stats["errors"] += 1
                    continue
                price = _quote_float(quote, "price", "current_price", "last_price")
                if price is None or price <= 0:
                    stats["errors"] += 1
                    continue
                stats["quotes"] += 1
                name = _quote_text(quote, "name", "stock_name") or code
                change_pct = _quote_float(quote, "change_pct", "change_percent", "pct_chg", "change_rate")
                priority, weight = self._priority(code, position, price)

                previous_price = _safe_float(symbol_state.get("last_price"))
                previous_day = str(symbol_state.get("last_day") or "")
                previous_change = _safe_float(symbol_state.get("last_change_pct"))
                if previous_day != today:
                    previous_change = 0.0

                technical = self._daily_levels(code, symbol_state, today)
                for level in self._levels_for(code, position, priority, technical):
                    event_key = f"price:{level.key}"
                    if self._crossed(previous_price, price, level):
                        stats["events"] += 1
                        if not self._already_notified_today(symbol_state, event_key, today):
                            if self._notify_price_level(
                                code=code,
                                name=name,
                                position=position,
                                price=price,
                                change_pct=change_pct,
                                weight=weight,
                                priority=priority,
                                level=level,
                            ):
                                self._mark_notified(symbol_state, event_key, today)
                                stats["sent"] += 1

                move_threshold = {"S": 3.0, "A": 4.0, "B": 5.0}[priority]
                if change_pct is not None:
                    for direction, threshold in (("up", move_threshold), ("down", -move_threshold)):
                        event_key = f"change:{direction}:{move_threshold:.1f}"
                        crossed = (
                            previous_change is not None
                            and (
                                (direction == "up" and previous_change < threshold <= change_pct)
                                or (direction == "down" and previous_change > threshold >= change_pct)
                            )
                        )
                        if crossed:
                            stats["events"] += 1
                            if not self._already_notified_today(symbol_state, event_key, today):
                                if self._notify_change(
                                    code=code,
                                    name=name,
                                    position=position,
                                    price=price,
                                    change_pct=change_pct,
                                    weight=weight,
                                    priority=priority,
                                    direction=direction,
                                    threshold=move_threshold,
                                ):
                                    self._mark_notified(symbol_state, event_key, today)
                                    stats["sent"] += 1

                symbol_state["last_price"] = price
                symbol_state["last_change_pct"] = change_pct
                symbol_state["last_day"] = today
                symbol_state["last_seen_at"] = now.isoformat()
                symbol_state["priority"] = priority
            except Exception as exc:
                stats["errors"] += 1
                logger.warning("Monitor cycle failed for one position: %s", exc)

        self.state.save()
        return stats


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Low-noise portfolio intraday monitor")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="Run one polling cycle (default)")
    mode.add_argument("--daemon", action="store_true", help="Run continuously")
    parser.add_argument("--interval", type=int, default=120, help="Polling interval in seconds for daemon mode")
    parser.add_argument("--state", default=os.getenv("PORTFOLIO_MONITOR_STATE", DEFAULT_STATE_PATH))
    parser.add_argument("--dry-run", action="store_true", help="Evaluate but never send PushPlus messages")
    parser.add_argument("--force", action="store_true", help="Poll even outside A-share trading windows")
    parser.add_argument("--verbose", action="store_true", help="Allow extra local diagnostics")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        monitor = IntradayPortfolioMonitor(state_path=args.state, dry_run=args.dry_run, verbose=args.verbose)
    except Exception as exc:
        print(f"monitor_init=FAIL reason={exc}", file=sys.stderr)
        return 2

    if not args.daemon:
        stats = monitor.run_once(force=args.force)
        print(
            "monitor_cycle=PASS "
            f"positions={stats['positions']} quotes={stats['quotes']} "
            f"events={stats['events']} sent={stats['sent']} errors={stats['errors']}"
        )
        return 0 if stats["errors"] < stats["positions"] else 1

    interval = max(30, int(args.interval))
    print(f"monitor_daemon=START interval_seconds={interval}")
    while True:
        try:
            stats = monitor.run_once(force=args.force)
            if args.verbose and stats["events"]:
                print(
                    "monitor_cycle=PASS "
                    f"positions={stats['positions']} quotes={stats['quotes']} "
                    f"events={stats['events']} sent={stats['sent']} errors={stats['errors']}"
                )
        except KeyboardInterrupt:
            print("monitor_daemon=STOP")
            return 0
        except Exception as exc:
            logger.exception("Daemon cycle failed: %s", exc)
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
