"""Daily open/close, dividend-adjusted, for the Instruments that were Signals,
plus the benchmarks: the TAIEX total-return index, and for Overseas
Instruments their own market's index.

The total-return index (TWSE MFI94U) is published as a close only. Its open is
taken as the price index's open scaled by that day's total-return/price ratio,
which already carries any dividend that went ex that morning."""
from __future__ import annotations

import re
from datetime import date, timedelta

import pandas as pd
import requests

from . import config


def _roc(s: str) -> date | None:
    m = re.match(r"(\d{2,3})/(\d{1,2})/(\d{1,2})", s.strip())
    if not m:
        return None
    y, mo, d = map(int, m.groups())
    return date(y + 1911, mo, d)


def _months(start: date, end: date):
    d = date(start.year, start.month, 1)
    while d <= end:
        yield d
        d = date(d.year + (d.month == 12), d.month % 12 + 1, 1)


def _taiex_tr(start: date, end: date) -> dict[str, float]:
    out = {}
    for m in _months(start, end):
        j = requests.get("https://www.twse.com.tw/rwd/zh/TAIEX/MFI94U",
                         params={"date": m.strftime("%Y%m%d"), "response": "json"},
                         headers=config.HTTP_HEADERS, timeout=config.HTTP_TIMEOUT).json()
        for row in j.get("data") or []:
            d = _roc(row[0])
            if d:
                out[d.isoformat()] = float(row[1].replace(",", ""))
    return out


def _yf(symbols: list[str], start: date, end: date) -> dict[str, pd.DataFrame]:
    import yfinance as yf
    if not symbols:
        return {}
    df = yf.download(symbols, start=start.isoformat(), end=(end + timedelta(days=1)).isoformat(),
                     auto_adjust=True, group_by="ticker", progress=False, threads=True)
    out = {}
    for s in symbols:
        try:
            sub = df[s] if isinstance(df.columns, pd.MultiIndex) else df
        except KeyError:
            continue
        sub = sub[["Open", "Close"]].dropna()
        if len(sub):
            out[s] = sub
    return out


def _last_day(con, symbol: str) -> str | None:
    r = con.execute("SELECT MAX(day) FROM prices WHERE symbol=?", (symbol,)).fetchone()
    return r[0]


def refresh(con, codes: set[str], start: date, log=print) -> None:
    end = date.today()
    # Benchmark: re-read the last month every time, TWSE revises late.
    last = _last_day(con, config.BENCHMARK)
    b_start = start if last is None else date.fromisoformat(last) - timedelta(days=31)
    tr = _taiex_tr(b_start, end)
    px = _yf(["^TWII"], b_start, end).get("^TWII")
    rows = []
    if px is not None:
        for ts, r in px.iterrows():
            d = ts.date().isoformat()
            if d in tr:
                rows.append((config.BENCHMARK, d, r["Open"] * tr[d] / r["Close"], tr[d]))
    con.executemany("INSERT OR REPLACE INTO prices VALUES(?,?,?,?)", rows)
    log(f"  prices: benchmark {len(rows)} days")
    for name, symbol in config.OVERSEAS_BENCHMARKS.values():
        last = _last_day(con, name)
        o_start = start if last is None else date.fromisoformat(last) - timedelta(days=31)
        df = _yf([symbol], o_start, end).get(symbol)
        if df is not None:
            con.executemany("INSERT OR REPLACE INTO prices VALUES(?,?,?,?)",
                            [(name, ts.date().isoformat(), float(r["Open"]), float(r["Close"]))
                             for ts, r in df.iterrows()])

    sym = {r["code"]: r["yf_symbol"] for r in con.execute(
        "SELECT code, yf_symbol FROM instruments WHERE yf_symbol IS NOT NULL")}
    # Adjusted history changes whenever a dividend is paid, so a symbol is
    # re-read in full; only ones never read or not read today are fetched.
    stale = [c for c in codes if c in sym and (_last_day(con, c) or "") < (end - timedelta(days=1)).isoformat()]
    for i in range(0, len(stale), 50):
        chunk = stale[i:i + 50]
        got = _yf([sym[c] for c in chunk], start, end)
        for c in chunk:
            df = got.get(sym[c])
            if df is None:
                continue
            con.execute("DELETE FROM prices WHERE symbol=?", (c,))
            con.executemany("INSERT INTO prices VALUES(?,?,?,?)",
                            [(c, ts.date().isoformat(), float(r["Open"]), float(r["Close"]))
                             for ts, r in df.iterrows()])
        con.commit()
    log(f"  prices: {len(stale)} Instruments refreshed")


def series(con, symbol: str) -> dict[str, tuple[float, float]]:
    return {r["day"]: (r["open"], r["close"]) for r in con.execute(
        "SELECT day, open, close FROM prices WHERE symbol=? ORDER BY day", (symbol,))}
