"""What followed each Signal: Excess Return from Entry over every Horizon,
summarised by Signal kind and Stance Group."""
from __future__ import annotations

import bisect
import statistics

from . import config
from .measure import net_stance, stance_group


def forward_return(px: dict[str, tuple[float, float]], days: list[str], entry_i: int, h: int) -> float | None:
    """Entry open to the close h trading days later (1D = the Entry day's own close)."""
    exit_i = entry_i + h - 1
    if exit_i >= len(days):
        return None
    o = px.get(days[entry_i], (None, None))[0]
    c = px.get(days[exit_i], (None, None))[1]
    if not o or not c:
        return None
    return c / o - 1


def entry_index(trading_days: list[str], forum_day: str) -> int:
    """The first trading day strictly after the Forum Day."""
    return bisect.bisect_right(trading_days, forum_day)


def run(sig: dict, tallies: dict, price_of, bench: dict) -> list[dict]:
    """One row per (Signal kind, Instrument, Forum Day): Stance and returns.
    `price_of(code)` returns a price series; the Market is judged on its own
    return rather than an Excess Return, which would be zero."""
    days = sorted(bench)
    rows = []
    cache: dict[str, dict] = {}
    for day, s in sorted(sig.items()):
        entry_i = entry_index(days, day)
        if entry_i >= len(days):
            continue
        for kind, codes in (("Top Mentioned", s["top"]), ("Buzz Spike", s["spike"]),
                            ("Market", s.get("market", set()))):
            for code in codes:
                px = cache.setdefault(code, price_of(code))
                if not px:
                    continue
                t = tallies.get((day, code))
                row = {"kind": kind, "code": code, "day": day, "entry": days[entry_i],
                       "net_stance": net_stance(t) if t else None, "group": stance_group(t)}
                for name, h in config.HORIZONS.items():
                    r = forward_return(px, days, entry_i, h)
                    b = forward_return(bench, days, entry_i, h)
                    row[name] = None if r is None or b is None else (r if code == config.MARKET else r - b)
                rows.append(row)
    return rows


def summarise(rows: list[dict]) -> list[dict]:
    """n, mean, median and share beating the Market, per kind × group × Horizon."""
    out = []
    for kind in ("Top Mentioned", "Buzz Spike", "Market"):
        for group in ("All", "Bullish", "Split", "Bearish"):
            sel = [r for r in rows if r["kind"] == kind and (group == "All" or r["group"] == group)]
            for name in config.HORIZONS:
                vals = [r[name] for r in sel if r[name] is not None]
                out.append({"kind": kind, "group": group, "horizon": name, "n": len(vals),
                            "mean": statistics.fmean(vals) if vals else None,
                            "median": statistics.median(vals) if vals else None,
                            "hit": sum(v > 0 for v in vals) / len(vals) if vals else None})
    return out
