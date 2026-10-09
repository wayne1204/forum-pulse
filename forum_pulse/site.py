"""Writes the dashboard: static pages plus one data file per Forum Day and per
Instrument, as JS so the pages also open straight from disk (file://)."""
from __future__ import annotations

import json
import shutil
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from . import config, prices
from .backtest import returns_from, summarise
from .instruments import NOT
from .measure import net_stance, stance_group

WEB = Path(__file__).parent / "web"
ROWS_PER_DAY = 30


def _write(path: Path, key: str, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"FP.loaded({json.dumps(key)}, {json.dumps(payload, ensure_ascii=False)});\n",
                    encoding="utf-8")


def _comments(con, day: str, codes: list[str]) -> dict:
    """Every push naming one of `codes` on `day`, oldest first, each with its
    user's Stance on that Instrument. Post titles are listed once."""
    q = ",".join("?" * len(codes))
    rows = con.execute(f"""
        SELECT h.code, c.user, c.tag, c.text, c.post_id, p.title, s.stance FROM hits h
        JOIN comments c ON c.id=h.comment_id JOIN posts p USING(post_id)
        LEFT JOIN stances s ON s.forum=c.forum AND s.day=c.day AND s.user=c.user AND s.code=h.code
        WHERE c.day=? AND h.code IN ({q}) AND c.seq>0
        GROUP BY h.code, c.id ORDER BY c.id""", (day, *codes)).fetchall()
    posts, by_code = {}, defaultdict(list)
    for r in rows:
        posts[r["post_id"]] = r["title"]
        by_code[r["code"]].append([r["user"], r["tag"], r["text"], r["post_id"], r["stance"]])
    return {"url": f"{config.PTT_BASE}/bbs/{config.PTT_BOARD}/", "posts": posts, "by_code": by_code}


def build(con, counts: dict, sig: dict, tallies: dict, bt_rows: list[dict], log=print) -> None:
    out = config.SITE_DIR
    out.mkdir(parents=True, exist_ok=True)
    for f in WEB.iterdir():
        shutil.copy(f, out / f.name)

    names = {r["code"]: r["name"] for r in con.execute("SELECT code, name FROM instruments")}
    # The Day table shows plain returns from Entry, with TAIEX as its baseline
    # row; Excess Returns stay on the Backtest and Instrument pages.
    exchange = dict(con.execute("SELECT code, exchange FROM instruments").fetchall())
    calendars = {b: sorted(prices.series(con, b))
                 for b in [config.BENCHMARK] + [n for n, _ in config.OVERSEAS_BENCHMARKS.values()]}
    px_cache: dict[str, dict] = {config.MARKET: prices.series(con, config.BENCHMARK)}

    def returns(code: str, day: str) -> dict | None:
        if code not in px_cache:
            px_cache[code] = prices.series(con, code)
        if not px_cache[code]:
            return None
        return returns_from(px_cache[code], calendars[config.benchmark_of(exchange.get(code, ""))], day)
    comments_per_day = {r["day"]: r["n"] for r in con.execute(
        "SELECT day, COUNT(*) n FROM comments GROUP BY day")}
    queued = dict(con.execute("""SELECT c.day, COUNT(*) FROM hits h JOIN comments c ON c.id=h.comment_id
                                 WHERE h.code IS NULL GROUP BY c.day""").fetchall())
    today = datetime.now(config.TZ).date().isoformat()
    days = sorted(d for d in counts if config.BACKFILL_START.isoformat() <= d <= today)
    featured: set[str] = set()

    for day in days:
        by_code = counts[day]
        s = sig.get(day, {"top": set(), "spike": set()})
        ranked = sorted(((n, c) for c, n in by_code.items() if c != config.MARKET), reverse=True)
        keep = [c for _, c in ranked[:ROWS_PER_DAY]] + sorted(s["spike"] - {c for _, c in ranked[:ROWS_PER_DAY]})
        rank = {c: i + 1 for i, (_, c) in enumerate(ranked)}

        def row(code):
            t = tallies.get((day, code))
            return {"code": code, "name": names.get(code, code), "mentions": by_code.get(code, 0),
                    "rank": rank.get(code), "top": code in s["top"], "spike": code in s["spike"],
                    "tally": t, "net": net_stance(t) if t else None, "group": stance_group(t),
                    "ret": returns(code, day)}

        rows = [row(c) for c in keep]
        featured.update(keep[:ROWS_PER_DAY])
        _write(out / "data" / "day" / f"{day}.js", f"day/{day}", {
            "day": day, "comments": comments_per_day.get(day, 0),
            "mentions": sum(by_code.values()), "queued": queued.get(day, 0),
            "market": row(config.MARKET) if config.MARKET in by_code else None,
            "baseline": returns(config.MARKET, day),       # TAIEX total return
            "rows": rows})
        _write(out / "data" / "cmt" / f"{day}.js", f"cmt/{day}",
               _comments(con, day, keep + [config.MARKET]))

    # Instrument pages, for everything that ever made a day's table.
    featured.add(config.MARKET)
    px = defaultdict(dict)
    for r in con.execute("SELECT symbol, day, close FROM prices"):
        px[r["symbol"]][r["day"]] = r["close"]
    for code in featured:
        series = []
        sym = config.BENCHMARK if code == config.MARKET else code
        for day in days:
            t = tallies.get((day, code))
            series.append([day, counts[day].get(code, 0), net_stance(t) if t else None,
                           px[sym].get(day), code in sig.get(day, {}).get("top", ()),
                           code in sig.get(day, {}).get("spike", ())])
        _write(out / "data" / "inst" / f"{code}.js", f"inst/{code}",
               {"code": code, "name": names.get(code, code), "series": series,
                "signals": [r for r in bt_rows if r["code"] == code]})

    agree = con.execute("""SELECT COUNT(*) n, SUM(stance=model_stance) ok FROM stances
                           WHERE source='author' AND model_stance IS NOT NULL""").fetchone()
    _write(out / "data" / "backtest.js", "backtest", {
        "summary": summarise(bt_rows), "horizons": list(config.HORIZONS),
        "signals": [dict(r, name=names.get(r["code"], r["code"])) for r in bt_rows],
        "model_check": {"n": agree["n"], "agree": agree["ok"] or 0}})
    _write(out / "data" / "meta.js", "meta", {
        "days": days, "built": datetime.now(config.TZ).isoformat(timespec="minutes"),
        "instruments": sorted(([c, names.get(c, c)] for c in featured), key=lambda x: x[0]),
        "config": {"top_n": config.TOP_N, "spike_ratio": config.SPIKE_RATIO,
                   "spike_min": config.SPIKE_MIN_MENTIONS, "cutoff": config.STANCE_GROUP_CUTOFF,
                   "min_decided": config.STANCE_GROUP_MIN_DECIDED}})
    log(f"  site: {len(days)} days, {len(featured)} instruments -> {out}")
