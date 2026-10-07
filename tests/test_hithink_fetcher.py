# -*- coding: utf-8 -*-

from unittest.mock import MagicMock

import pandas as pd
import pytest

from data_provider.base import DataFetchError
from data_provider.hithink_fetcher import HiThinkFetcher


def test_hithink_to_thscode_shanghai():
    assert HiThinkFetcher.to_thscode("600096") == "600096.SH"
    assert HiThinkFetcher.to_thscode("sh600096") == "600096.SH"
    assert HiThinkFetcher.to_thscode("600096.SH") == "600096.SH"


def test_hithink_to_thscode_shenzhen():
    assert HiThinkFetcher.to_thscode("002971") == "002971.SZ"
    assert HiThinkFetcher.to_thscode("sz002971") == "002971.SZ"
    assert HiThinkFetcher.to_thscode("002971.SZ") == "002971.SZ"


def test_hithink_to_thscode_beijing():
    assert HiThinkFetcher.to_thscode("920001") == "920001.BJ"
    assert HiThinkFetcher.to_thscode("BJ920001") == "920001.BJ"


def test_hithink_rejects_invalid_code():
    with pytest.raises(ValueError):
        HiThinkFetcher.to_thscode("ABC")


def test_hithink_missing_api_key_raises():
    fetcher = HiThinkFetcher(api_key="")

    fetcher.api_key = ""

    with pytest.raises(DataFetchError):
        fetcher._headers()


def test_hithink_historical_kline_normalization():
    fetcher = HiThinkFetcher(api_key="test-key")

    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "code": 0,
        "message": "success",
        "request_id": "test-request",
        "data": {
            "timestamp": 1788192000000,
            "item": [
                {
                    "date_ms": 1788192000000,
                    "open_price": 10.00,
                    "high_price": 10.80,
                    "low_price": 9.90,
                    "close_price": 10.50,
                    "volume": 100000,
                    "turnover": 1030000.0,
                },
                {
                    "date_ms": 1788278400000,
                    "open_price": 10.50,
                    "high_price": 11.00,
                    "low_price": 10.30,
                    "close_price": 10.80,
                    "volume": 120000,
                    "turnover": 1290000.0,
                },
            ],
        },
    }

    fetcher.session.get = MagicMock(return_value=response)

    df = fetcher.get_daily_data(
        "600096",
        start_date="2026-09-01",
        end_date="2026-09-02",
    )

    assert isinstance(df, pd.DataFrame)
    assert len(df) == 2

    expected_columns = {
        "date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
        "pct_chg",
        "ma5",
        "ma10",
        "ma20",
        "volume_ratio",
    }

    assert expected_columns.issubset(set(df.columns))

    assert float(df.iloc[0]["close"]) == 10.50
    assert float(df.iloc[1]["close"]) == 10.80

    request = fetcher.session.get.call_args

    assert (
        request.args[0]
        == "https://fuyao.aicubes.cn/api/a-share/prices/historical"
    )

    assert request.kwargs["params"]["thscode"] == "600096.SH"
    assert request.kwargs["params"]["interval"] == "1d"
    assert request.kwargs["params"]["adjust"] == "forward"

    assert request.kwargs["headers"]["X-api-key"] == "test-key"


def test_hithink_api_business_error():
    fetcher = HiThinkFetcher(api_key="test-key")

    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "code": 2003,
        "message": "invalid api key",
        "request_id": "error-request",
        "data": None,
    }

    fetcher.session.get = MagicMock(return_value=response)

    with pytest.raises(DataFetchError) as exc_info:
        fetcher._get(
            "/api/a-share/prices/historical",
            {
                "thscode": "600096.SH",
                "interval": "1d",
                "start": 1,
                "end": 2,
                "adjust": "forward",
            },
        )

    assert "code=2003" in str(exc_info.value)


def test_hithink_empty_history_returns_empty_dataframe():
    fetcher = HiThinkFetcher(api_key="test-key")

    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "code": 0,
        "message": "success",
        "data": {
            "timestamp": None,
            "item": [],
        },
    }

    fetcher.session.get = MagicMock(return_value=response)

    df = fetcher.get_daily_data(
        "002971",
        start_date="2026-09-01",
        end_date="2026-09-02",
    )

    assert df.empty
