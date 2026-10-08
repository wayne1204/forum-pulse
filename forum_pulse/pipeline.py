"""The daily run, end to end, and the lighter rebuild used after a Review Queue
decision."""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta

from . import backtest, config, db, instruments, measure, prices, ptt, site, stance


def _log(msg):
    print(f"{datetime.now(config.TZ):%H:%M:%S} {msg}", flush=True)


def _crawl_since(con) -> date:
    """Until one crawl has reached BACKFILL_START, every run walks back that
    far (stored Posts are skipped), so an interrupted backfill heals itself."""
    if db.get_meta(con, "backfill_done") != "1":
        return config.BACKFILL_START
    newest = con.execute("SELECT MAX(posted_at) FROM posts").fetchone()[0]
    recent = date.today() - timedelta(days=config.RECRAWL_DAYS)
    return min(date.fromisoformat(newest[:10]), recent) if newest else config.BACKFILL_START


def _featured_codes(counts, sig) -> set[str]:
    codes = set()
    for day, by_code in counts.items():
        ranked = sorted(((n, c) for c, n in by_code.items() if c != config.MARKET), reverse=True)
        codes |= {c for _, c in ranked[:site.ROWS_PER_DAY]}
        codes |= sig.get(day, {}).get("spike", set())
    return codes


def _derive_and_publish(con, fetch_prices: bool, log=_log):
    counts = measure.daily_counts(con)
    sig = measure.signals(counts)
    if fetch_prices:
        prices.refresh(con, _featured_codes(counts, sig), config.BACKFILL_START, log=log)
        con.commit()
    tallies = measure.stance_tally(con)
    bench = prices.series(con, config.BENCHMARK)
    rows = backtest.run(sig, tallies, lambda c: prices.series(con, config.BENCHMARK if c == config.MARKET else c), bench)
    log(f"backtest: {len(rows)} Signal rows")
    site.build(con, counts, sig, tallies, rows, log=log)


def daily(label: bool = True, budget: float | None = None, log=_log) -> None:
    t0 = time.time()
    with db.session() as con:
        last = db.get_meta(con, "instruments_at")
        if not last or (datetime.now() - datetime.fromisoformat(last)).days >= 7:
            n = instruments.refresh(con)
            db.set_meta(con, "instruments_at", datetime.now().isoformat(timespec="seconds"))
            con.execute("UPDATE comments SET matched=0")      # new Aliases: search again
            log(f"instruments: {n} listed")
            con.commit()

        since = _crawl_since(con)
        log(f"crawl: PTT Stock since {since}")
        changed = ptt.crawl(con, since, log=log)
        if since == config.BACKFILL_START:
            db.set_meta(con, "backfill_done", "1")
        con.commit()
        log(f"crawl: {changed} Comments new or changed")

        n = measure.match_comments(con, log=log)
        log(f"match: {n} Comments searched")
        log(f"mentions: {measure.build_mentions(con)}")
        log(f"author stances: {measure.author_stances(con)}")
        con.commit()
        if label:
            stance.label(con, budget=budget, log=log)
            con.commit()
        _derive_and_publish(con, fetch_prices=True, log=log)
    log(f"done in {time.time() - t0:.0f}s")


def rebuild(log=_log) -> None:
    """Apply Review Queue decisions and republish; no crawling, labelling or
    price fetching."""
    with db.session() as con:
        measure.reresolve(con)
        measure.build_mentions(con)
        measure.author_stances(con)
        con.commit()
        _derive_and_publish(con, fetch_prices=False, log=log)


def label_only(since: str | None = None, max_mentions: int | None = None,
               workers: int | None = None, log=_log) -> None:
    """Label a chosen range of Forum Days, then republish. No crawl, no prices."""
    if workers:
        config.LLM_WORKERS = workers
    if since:
        config.LLM_LABEL_FROM = date.fromisoformat(since)
    if max_mentions is not None:
        config.LLM_CLI_MAX_PER_RUN = max_mentions or 10**9
    t0 = time.time()
    with db.session() as con:
        stance.label(con, log=log)
        con.commit()
        _derive_and_publish(con, fetch_prices=False, log=log)
    log(f"done in {time.time() - t0:.0f}s")
