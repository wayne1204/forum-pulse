import json
import re
import sys
from datetime import date, timedelta

import pytest

from conftest import DAY, LISTED, SLANG, add_post
from forum_pulse import __main__ as cli
from forum_pulse import config, db, instruments, measure, pipeline, server, site

PID = "M.1790920910.A.717"
quiet = lambda *_: None


def read_js(path):
    m = re.fullmatch(r'FP\.loaded\(("[^"]*"), (.*)\);\n', path.read_text(encoding="utf-8"), re.S)
    return json.loads(m.group(1)), json.loads(m.group(2))


PRICES = [  # symbol, day, open, close
    (config.BENCHMARK, "2026-10-01", 100.0, 100.0), (config.BENCHMARK, "2026-10-02", 100.0, 100.0),
    (config.BENCHMARK, "2026-10-05", 100.0, 101.0), (config.BENCHMARK, "2026-10-06", 101.0, 102.0),
    ("2330", "2026-10-02", 95.0, 99.0), ("2330", "2026-10-05", 100.0, 110.0), ("2330", "2026-10-06", 110.0, 111.0),
]


@pytest.fixture
def offline(monkeypatch):
    """The daily run with the network cut: the listing, PTT and prices are canned."""
    refresh = instruments.refresh
    monkeypatch.setattr(instruments, "refresh", lambda con: refresh(con, LISTED, SLANG))
    calls = {"crawl": [], "label": [], "decide": []}
    monkeypatch.setattr(pipeline.alias_calls, "decide",
                        lambda con, log: calls["decide"].append(True) or {"quota": False})

    def crawl(con, since, log):
        calls["crawl"].append(since)
        add_post(con, PID, "[標的] 2330 台積電 多", "bull", DAY,
                 [("u1", "GG 今天一定噴上天"), ("u2", "大盤要崩")])
        return 3
    monkeypatch.setattr(pipeline.ptt, "crawl", crawl)
    monkeypatch.setattr(pipeline.prices, "refresh",
                        lambda con, codes, start, log: con.executemany(
                            "INSERT OR REPLACE INTO prices VALUES(?,?,?,?)", PRICES))
    monkeypatch.setattr(pipeline.stance, "label", lambda con, budget, log: calls["label"].append(budget))
    return calls


def test_daily_crawls_measures_and_publishes(offline):
    pipeline.daily(budget=1.5, log=quiet)
    assert offline["crawl"] == [config.BACKFILL_START]
    assert offline["label"] == [1.5] and offline["decide"] == [True]
    with db.session() as con:
        assert db.get_meta(con, "backfill_done") == "1"
        assert db.get_meta(con, "instruments_at")

    out = config.SITE_DIR
    assert (out / "index.html").exists()
    key, day = read_js(out / "data" / "day" / "2026-10-02.js")
    assert key == "day/2026-10-02" and day["comments"] == 3 and day["mentions"] == 3
    [row] = day["rows"]
    assert (row["code"], row["rank"], row["top"], row["net"]) == ("2330", 1, True, 1.0)
    assert row["ret"]["1D"] == pytest.approx(0.10) and row["ret"]["1M"] is None   # plain return
    assert day["baseline"]["1D"] == pytest.approx(0.01)                              # TAIEX TR
    assert day["market"]["code"] == config.MARKET
    key, cmt = read_js(out / "data" / "cmt" / "2026-10-02.js")
    assert key == "cmt/2026-10-02" and cmt["posts"] == {PID: "[標的] 2330 台積電 多"}
    assert cmt["by_code"] == {"2330": [["u1", "推", "GG 今天一定噴上天", PID, None]],
                              config.MARKET: [["u2", "推", "大盤要崩", PID, None]]}

    _, inst = read_js(out / "data" / "inst" / "2330.js")
    assert inst["series"] == [["2026-10-02", 2, 1.0, 99.0, True, False]]
    _, bt = read_js(out / "data" / "backtest.js")
    assert {s["code"] for s in bt["signals"]} == {"2330", config.MARKET}
    assert bt["model_check"] == {"n": 0, "agree": 0}
    _, meta = read_js(out / "data" / "meta.js")
    assert meta["days"] == ["2026-10-02"]
    assert meta["instruments"] == [["2330", "台積電"], [config.MARKET, "大盤"]]


def test_after_the_backfill_only_recent_posts_are_crawled(offline):
    pipeline.daily(label=False, log=quiet)
    pipeline.daily(label=False, log=quiet)
    recent = date.today() - timedelta(days=config.RECRAWL_DAYS)
    assert offline["crawl"] == [config.BACKFILL_START, min(DAY.date(), recent)]
    assert offline["label"] == [] and offline["decide"] == []      # no model at all


def test_rebuild_applies_review_queue_rules(world):
    add_post(world, PID, "[閒聊] 盤中", "host", DAY, [("u1", "長榮噴")])
    measure.match_comments(world, log=quiet)       # as the daily run left it: 長榮 queued
    world.execute("INSERT INTO alias_rules(alias, scope, code) VALUES('長榮', 'always', '2618')")
    world.commit()
    pipeline.rebuild(log=quiet)
    assert [tuple(r) for r in world.execute("SELECT user, code FROM mentions")] == [("u1", "2618")]
    assert (config.SITE_DIR / "data" / "day" / "2026-10-02.js").exists()


def test_label_only_sets_the_range_then_republishes(world, monkeypatch):
    for k in ("LLM_LABEL_FROM", "LLM_CLI_MAX_PER_RUN", "LLM_WORKERS"):
        monkeypatch.setattr(config, k, getattr(config, k))
    order = []
    monkeypatch.setattr(pipeline.alias_calls, "decide", lambda con, log: order.append("decide") or {"quota": False})
    monkeypatch.setattr(pipeline.stance, "label", lambda con, log: order.append("label"))
    pipeline.label_only("2026-09-01", 0, 2, log=quiet)
    assert order == ["decide", "label"]                 # Mentions settle before they are labelled
    assert config.LLM_LABEL_FROM == date(2026, 9, 1)
    assert config.LLM_CLI_MAX_PER_RUN == 10**9       # 0 = no cap
    assert config.LLM_WORKERS == 2
    assert (config.SITE_DIR / "data" / "meta.js").exists()


def test_the_usage_limit_in_alias_calls_skips_labelling(world, monkeypatch):
    order = []
    monkeypatch.setattr(pipeline.alias_calls, "decide", lambda con, log: order.append("decide") or {"quota": True})
    monkeypatch.setattr(pipeline.stance, "label", lambda con, log: order.append("label"))
    pipeline.label_only(log=quiet)
    assert order == ["decide"]


def test_featured_codes_are_the_tables_and_the_spikes():
    counts = {"2026-10-02": {"2330": 5, config.MARKET: 9}}
    assert pipeline._featured_codes(counts, {"2026-10-02": {"spike": {"2603"}}}) == {"2330", "2603"}


def test_site_leaves_out_days_before_the_backfill(con):
    counts = {"2020-01-02": {"2330": 1}}
    site.build(con, counts, {}, {}, [], log=quiet)
    _, meta = read_js(config.SITE_DIR / "data" / "meta.js")
    assert meta["days"] == []


# --- the command line ------------------------------------------------------------------

@pytest.mark.parametrize("argv, want", [
    (["daily"], ("daily", (), {"label": True, "budget": None})),
    (["daily", "--no-label", "--budget", "2"], ("daily", (), {"label": False, "budget": 2.0})),
    (["label", "--from", "2026-09-01", "--max", "0", "--workers", "8"], ("label", ("2026-09-01", 0, 8), {})),
    (["rebuild"], ("rebuild", (), {})),
    (["serve"], ("serve", (8765,), {})),
    (["serve", "9000"], ("serve", (9000,), {})),
])
def test_command_line(monkeypatch, argv, want):
    got = []
    rec = lambda name: lambda *a, **kw: got.append((name, a, kw))
    monkeypatch.setattr(pipeline, "daily", rec("daily"))
    monkeypatch.setattr(pipeline, "label_only", rec("label"))
    monkeypatch.setattr(pipeline, "rebuild", rec("rebuild"))
    monkeypatch.setattr(server, "serve", rec("serve"))
    monkeypatch.setattr(sys, "argv", ["forum_pulse", *argv])
    cli.main()
    assert got == [want]
