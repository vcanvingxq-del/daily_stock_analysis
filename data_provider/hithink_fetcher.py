# -*- coding: utf-8 -*-
"""HiThink Financial API data provider for A-share market data."""

from __future__ import annotations

import os
from datetime import datetime, time
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from .base import (
    BaseFetcher,
    DataFetchError,
    STANDARD_COLUMNS,
    is_bse_code,
    normalize_stock_code,
)


class HiThinkFetcher(BaseFetcher):
    """Fetch A-share market data from HiThink Financial API."""

    name = "HiThinkFetcher"
    priority = -2
    allow_empty_daily_data = True

    BASE_URL = "https://fuyao.aicubes.cn"
    HTTP_TIMEOUT_SECONDS = 15

    def __init__(
        self,
        api_key: Optional[str] = None,
        timeout: int = HTTP_TIMEOUT_SECONDS,
    ) -> None:
        self.api_key = (
            api_key
            or os.getenv("HITHINK_FINANCE_API_KEY")
            or ""
        ).strip()

        self.timeout = timeout
        self.session = requests.Session()

    @property
    def available(self) -> bool:
        """Return whether the API key is configured."""
        return bool(self.api_key)

    @staticmethod
    def to_thscode(stock_code: str) -> str:
        """Convert project stock code into HiThink thscode.

        Examples:
            600096      -> 600096.SH
            sh600096    -> 600096.SH
            600096.SH   -> 600096.SH
            002971      -> 002971.SZ
            920xxx      -> 920xxx.BJ
        """
        raw = str(stock_code or "").strip().upper()

        if not raw:
            raise ValueError("empty stock code")

        if raw.endswith((".SH", ".SZ", ".BJ")):
            code = raw[:-3]
            if len(code) != 6 or not code.isdigit():
                raise ValueError(f"invalid A-share code: {stock_code}")
            return raw

        for prefix in ("SH", "SZ", "BJ"):
            if raw.startswith(prefix):
                raw = raw[len(prefix):]
                break

        code = normalize_stock_code(raw)

        if not code or len(code) != 6 or not code.isdigit():
            raise ValueError(f"invalid A-share code: {stock_code}")

        if is_bse_code(code):
            return f"{code}.BJ"

        if code.startswith(("5", "6", "9")):
            return f"{code}.SH"

        if code.startswith(("0", "1", "2", "3")):
            return f"{code}.SZ"

        raise ValueError(
            f"cannot determine exchange for A-share code: {stock_code}"
        )

    def _headers(self) -> Dict[str, str]:
        if not self.api_key:
            raise DataFetchError(
                "HITHINK_FINANCE_API_KEY is not configured"
            )

        return {
            "X-api-key": self.api_key,
            "Accept": "application/json",
        }

    def _get(
        self,
        path: str,
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Perform a HiThink API request and unwrap ApiResponse."""
        response = self.session.get(
            f"{self.BASE_URL}{path}",
            params=params,
            headers=self._headers(),
            timeout=self.timeout,
        )

        response.raise_for_status()

        payload = response.json()

        if not isinstance(payload, dict):
            raise DataFetchError(
                "HiThink API returned an invalid response"
            )

        code = payload.get("code")

        if code != 0:
            message = payload.get("message") or "unknown error"
            request_id = payload.get("request_id")

            detail = f"HiThink API code={code}: {message}"

            if request_id:
                detail += f" (request_id={request_id})"

            raise DataFetchError(detail)

        data = payload.get("data")

        if not isinstance(data, dict):
            return {}

        return data

    @staticmethod
    def _date_to_ms(
        date_value: str,
        *,
        end_of_day: bool = False,
    ) -> int:
        """Convert YYYY-MM-DD to Asia/Shanghai Unix milliseconds."""
        tz = ZoneInfo("Asia/Shanghai")
        date_obj = datetime.strptime(date_value, "%Y-%m-%d").date()

        if end_of_day:
            dt = datetime.combine(
                date_obj,
                time(23, 59, 59, 999000),
                tzinfo=tz,
            )
        else:
            dt = datetime.combine(
                date_obj,
                time.min,
                tzinfo=tz,
            )

        return int(dt.timestamp() * 1000)

    def _fetch_raw_data(
        self,
        stock_code: str,
        start_date: str,
        end_date: str,
    ) -> pd.DataFrame:
        """Fetch forward-adjusted daily K-line data."""
        thscode = self.to_thscode(stock_code)

        data = self._get(
            "/api/a-share/prices/historical",
            {
                "thscode": thscode,
                "interval": "1d",
                "start": self._date_to_ms(start_date),
                "end": self._date_to_ms(
                    end_date,
                    end_of_day=True,
                ),
                "adjust": "forward",
            },
        )

        items = data.get("item") or []

        if not isinstance(items, list) or not items:
            return pd.DataFrame(columns=STANDARD_COLUMNS)

        return pd.DataFrame(items)

    def _normalize_data(
        self,
        df: pd.DataFrame,
        stock_code: str,
    ) -> pd.DataFrame:
        """Normalize HiThink K-line fields to project standard columns."""
        if df is None or df.empty:
            return pd.DataFrame(columns=STANDARD_COLUMNS)

        required = {
            "date_ms",
            "open_price",
            "high_price",
            "low_price",
            "close_price",
            "volume",
            "turnover",
        }

        missing = required.difference(df.columns)

        if missing:
            raise DataFetchError(
                "HiThink historical response missing fields: "
                + ", ".join(sorted(missing))
            )

        normalized = pd.DataFrame()

        dates = pd.to_datetime(
            df["date_ms"],
            unit="ms",
            utc=True,
            errors="coerce",
        )

        normalized["date"] = (
            dates.dt.tz_convert("Asia/Shanghai")
            .dt.tz_localize(None)
        )

        normalized["open"] = pd.to_numeric(
            df["open_price"],
            errors="coerce",
        )

        normalized["high"] = pd.to_numeric(
            df["high_price"],
            errors="coerce",
        )

        normalized["low"] = pd.to_numeric(
            df["low_price"],
            errors="coerce",
        )

        normalized["close"] = pd.to_numeric(
            df["close_price"],
            errors="coerce",
        )

        normalized["volume"] = pd.to_numeric(
            df["volume"],
            errors="coerce",
        )

        normalized["amount"] = pd.to_numeric(
            df["turnover"],
            errors="coerce",
        )

        normalized["pct_chg"] = (
            normalized["close"]
            .pct_change(fill_method=None)
            .fillna(0.0)
            * 100.0
        )

        return normalized[
            [
                "date",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "amount",
                "pct_chg",
            ]
        ]
