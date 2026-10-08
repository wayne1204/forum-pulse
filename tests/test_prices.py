import sys
from datetime import date, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

from forum_pulse import config, prices


def frame(rows):
    """{day: (open, close)} as yfinance returns it."""
    return pd.DataFrame({"Open": [o for o, _ in rows.values()], "Close": [c for _, c in rows.values()]},
                        index=pd.to_datetime(list(rows)))


def test_roc_dates():
    assert prices._roc("115/10/02") == date(2026, 10, 2)
    assert prices._roc(" 99/1/5") == date(2010, 1, 5)
    assert prices._roc("合計") is None


def test_months_cross_the_year():
    assert list(prices._months(date(2025, 11, 15), date(2026, 2, 1))) == \
        [date(2025, 11, 1), date(2025, 12, 1), date(2026, 1, 1), date(2026, 2, 1)]


def test_total_return_index_is_read_month_by_month(monkeypatch):
    asked = []

    def get(url, params, headers, timeout):
        asked.append(params["date"])
        data = [["115/09/30", "30,000.50"], ["說明", "x"]] if params["date"] == "20260901" else None
        return SimpleNamespace(json=lambda: {"data": data})
    monkeypatch.setattr(prices.requests, "get", get)
    assert prices._taiex_tr(date(2026, 9, 10), date(2026, 10, 3)) == {"2026-09-30": 30000.5}
    assert asked == ["20260901", "20261001"]


def test_yf_keeps_open_and_close_of_symbols_it_got(monkeypatch):
    def download(symbols, **kw):
        assert kw["auto_adjust"] is True
        f = frame({"2026-10-01": (10.0, 11.0), "2026-10-02": (None, None)})
        if len(symbols) == 1:
            return f
        return pd.concat({"2330.TW": f}, axis=1)        # 2603.TW: nothing came back
    monkeypatch.setitem(sys.modules, "yfinance", SimpleNamespace(download=download))
    got = prices._yf(["2330.TW", "2603.TW"], date(2026, 10, 1), date(2026, 10, 2))
    assert list(got) == ["2330.TW"] and len(got["2330.TW"]) == 1
    assert list(prices._yf(["^TWII"], date(2026, 10, 1), date(2026, 10, 2))) == ["^TWII"]
    assert prices._yf([], date(2026, 10, 1), date(2026, 10, 2)) == {}


def test_refresh_stores_the_benchmark_and_stale_instruments(con, monkeypatch):
    starts = []
    monkeypatch.setattr(prices, "_taiex_tr", lambda s, e: starts.append(s) or
                        {"2026-10-01": 20000.0, "2026-10-02": 20400.0})

    def yf(symbols, start, end):
        if symbols == ["^TWII"]:
            return {"^TWII": frame({"2026-10-01": (100.0, 100.0), "2026-10-02": (101.0, 102.0),
                                    "2026-10-03": (1.0, 1.0)})}
        return {"2330.TW": frame({"2026-10-01": (500.0, 510.0)})}
    monkeypatch.setattr(prices, "_yf", yf)

    prices.refresh(con, {"2330", "2603", "NOPE"}, date(2025, 7, 1), log=lambda *_: None)
    bench = prices.series(con, config.BENCHMARK)
    assert bench["2026-10-01"] == (20000.0, 20000.0)
    assert bench["2026-10-02"] == (pytest.approx(101 * 20400 / 102), 20400.0)   # open scaled to TR
    assert "2026-10-03" not in bench                                           # no TR close that day
    assert prices.series(con, "2330") == {"2026-10-01": (500.0, 510.0)}
    assert prices.series(con, "2603") == {}

    prices.refresh(con, set(), date(2025, 7, 1), log=lambda *_: None)
    assert starts == [date(2025, 7, 1), date(2026, 10, 2) - timedelta(days=31)]   # TWSE revises late
