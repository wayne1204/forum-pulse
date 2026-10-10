from datetime import datetime
from pathlib import Path

import pytest

from conftest import LISTED, SLANG, add_post
from forum_pulse import backtest, config, instruments, measure, ptt
from forum_pulse.instruments import NOT, Matcher, resolve

FIX = Path(__file__).parent / "fixtures"
TZ = config.TZ


# --- PTT parsing ----------------------------------------------------------------

def test_index_page_lists_posts_above_the_pinned_ones():
    posts, prev = ptt.parse_index((FIX / "index.html").read_text())
    assert prev == 10423
    assert all(p["post_id"].startswith("M.") for p in posts)


def test_post_body_and_pushes_are_separate_comments():
    p = ptt.parse_post((FIX / "post_target.html").read_text(), "M.1790943882.A.49A")
    assert p["title"] == "[標的] 2330 台積電 多"
    assert p["author"] == "PolarBearCat"
    assert "推" not in p["body"][:5] and "※ 發信站" not in p["body"]
    assert len(p["pushes"]) == 58
    assert p["pushes"][0]["at"] == datetime(2026, 10, 2, 20, 27, tzinfo=TZ)


def test_post_time_comes_from_its_id():
    assert ptt.post_time_from_id("M.1790920910.A.717").date().isoformat() == "2026-10-02"


@pytest.mark.parametrize("raw, posted, want", [
    (" 01/03 09:10", datetime(2025, 12, 30, tzinfo=TZ), datetime(2026, 1, 3, 9, 10, tzinfo=TZ)),
    (" 09/27 10:00", datetime(2026, 9, 28, tzinfo=TZ), datetime(2026, 9, 27, 10, 0, tzinfo=TZ)),
    (" 03/09 21:00", datetime(2025, 7, 1, tzinfo=TZ), datetime(2026, 3, 9, 21, 0, tzinfo=TZ)),
    ("", datetime(2026, 9, 28, 8, tzinfo=TZ), datetime(2026, 9, 28, 8, tzinfo=TZ)),
])
def test_push_year_is_inferred_from_the_post(raw, posted, want):
    assert ptt._push_time(raw, posted) == want


def test_post_type_reads_the_bracket_even_on_replies():
    assert ptt.post_type("Re: [標的] 2330 台積電 多") == "標的"
    assert ptt.post_type("[閒聊] 2026/10/02 盤後閒聊") == "閒聊"
    assert ptt.post_type("沒有分類") is None


def test_rereading_a_post_keeps_comment_ids(con):
    post = {"post_id": "M.1790943882.A.49A", "title": "[閒聊] x", "author": "a",
            "posted_at": datetime(2026, 10, 2, 9, tzinfo=TZ), "body": "b",
            "pushes": [{"tag": "推", "user": "u1", "text": "GG", "at": datetime(2026, 10, 2, 9, 5, tzinfo=TZ)}]}
    assert ptt.store_post(con, post) == 2
    ids = [r[0] for r in con.execute("SELECT id FROM comments ORDER BY seq")]
    post["pushes"].append({"tag": "噓", "user": "u2", "text": "x", "at": datetime(2026, 10, 2, 9, 6, tzinfo=TZ)})
    assert ptt.store_post(con, post) == 1
    assert [r[0] for r in con.execute("SELECT id FROM comments ORDER BY seq")][:2] == ids


# --- Aliases --------------------------------------------------------------------

@pytest.fixture
def matcher(con):
    return Matcher.from_db(con)


def test_codes_names_and_slang_are_found(matcher):
    assert matcher.find("GG 噴") == {"GG": {"2330"}}
    assert matcher.find("[標的] 2330 台積電 多") == {"2330": {"2330"}, "台積電": {"2330"}}
    assert matcher.find("買了00631L跟正2") == {"00631L": {"00631L"}, "正2": {"00631L"}}
    assert matcher.find("大盤要崩") == {"大盤": {"MARKET"}}


@pytest.mark.parametrize("text", ["2026/10/02 盤後閒聊", "台積電 2489附近撐住", "14:30 收盤", "收2489", "跌到2489元"])
def test_numbers_that_are_dates_times_or_prices_are_not_codes(matcher, text):
    assert "2489" not in matcher.find(text)


def test_longest_alias_wins(matcher):
    assert matcher.find("長榮航 好飛") == {"長榮航": {"2618"}}


def test_ambiguous_aliases_wait_for_the_user(matcher):
    assert matcher.find("長榮噴")["長榮"] == {"2603", "2618"}
    assert matcher.find("統一意見")["統一"] == {"1216", NOT}
    assert resolve("長榮", {"2603", "2618"}, "長榮噴", 1, {}) is None


def test_a_code_that_is_also_a_year_may_name_nothing(con):
    instruments.refresh(con, LISTED + [{"code": "2008", "name": "高興昌", "kind": "stock", "exchange": "TWSE",
                                        "yf_symbol": "2008.TW"}], SLANG)
    m = Matcher.from_db(con)
    assert m.find("崩回2008金融海嘯") == {"2008": {"2008", NOT}}
    assert m.find("2008 高興昌 買超") == {"2008": {"2008"}, "高興昌": {"2008"}}
    assert m.find("2330 噴")["2330"] == {"2330"}


def test_rules_resolve_most_specific_first():
    rules = {"長榮": [{"scope": "always", "code": "2603", "context": "", "comment_id": 0},
                      {"scope": "context", "code": "2618", "context": "航空", "comment_id": 0},
                      {"scope": "comment", "code": NOT, "context": "", "comment_id": 7}]}
    c = {"2603", "2618"}
    assert resolve("長榮", c, "長榮噴", 1, rules) == "2603"
    assert resolve("長榮", c, "長榮航空噴", 1, rules) == "2618"
    assert resolve("長榮", c, "長榮航空噴", 7, rules) == NOT


# --- Mentions and Signals -------------------------------------------------------------

def test_a_user_is_one_mention_per_instrument_per_day(con):
    when = datetime(2026, 10, 2, 10, tzinfo=TZ)
    add_post(con, "M.1790920910.A.717", "[閒聊] 盤中", "host", when,
          [("u1", "GG 噴"), ("u1", "台積電 噴噴噴"), ("u1", "2330 還要噴"), ("u2", "GG 倒"), ("u3", "長榮噴")])
    measure.match_comments(con, log=lambda *_: None)
    measure.build_mentions(con)
    rows = con.execute("SELECT user, code, n_comments FROM mentions ORDER BY user").fetchall()
    assert [tuple(r) for r in rows] == [("u1", "2330", 3), ("u2", "2330", 1)]   # 長榮 waits in the queue
    assert con.execute("SELECT COUNT(*) FROM hits WHERE code IS NULL").fetchone()[0] == 1


def test_author_stance_from_a_target_post_title(con):
    when = datetime(2026, 10, 2, 20, tzinfo=TZ)
    add_post(con, "M.1790943882.A.49A", "[標的] 2330 台積電 空", "bear", when, [])
    add_post(con, "M.1790943883.A.49B", "Re: [標的] 2330 台積電 空", "other", when, [])
    measure.match_comments(con, log=lambda *_: None)
    measure.build_mentions(con)
    assert measure.author_stances(con) >= 1
    rows = con.execute("SELECT user, stance, source FROM stances").fetchall()
    assert [tuple(r) for r in rows] == [("bear", "bearish", "author")]


def test_an_author_stance_is_only_for_the_instrument_in_the_title(con):
    when = datetime(2026, 10, 2, 20, tzinfo=TZ)
    add_post(con, "M.1790943882.A.49A", "[標的] 2489 瑞軒 多", "bull", when, [],
             body="比起台積電，瑞軒更有機會")
    measure.match_comments(con, log=lambda *_: None)
    measure.build_mentions(con)
    measure.author_stances(con)
    rows = con.execute("SELECT code, stance, source FROM stances").fetchall()
    assert [tuple(r) for r in rows] == [("2489", "bullish", "author")]


def test_a_stale_author_stance_gives_way_to_the_model_or_the_queue(con):
    when = datetime(2026, 10, 2, 20, tzinfo=TZ)
    add_post(con, "M.1790943882.A.49A", "[標的] 2489 瑞軒 多", "bull", when, [],
             body="比起台積電，瑞軒更有機會")
    measure.match_comments(con, log=lambda *_: None)
    measure.build_mentions(con)
    # written by the old rule: body Instruments got the title's Stance too
    con.execute("INSERT INTO stances VALUES('ptt', '2026-10-02', 'bull', '2330', 'bullish', 'author', 1, 'neutral')")
    con.execute("INSERT INTO stances VALUES('ptt', '2026-10-02', 'bull', '2603', 'bullish', 'author', 1, NULL)")
    measure.author_stances(con)
    rows = con.execute("SELECT code, stance, source, model_stance FROM stances ORDER BY code").fetchall()
    assert [tuple(r) for r in rows] == [("2330", "neutral", "model", None),
                                        ("2489", "bullish", "author", None)]


def test_top_mentioned_and_buzz_spike():
    counts = {f"2026-09-{d:02d}": {"2330": 100, "2603": 1} for d in range(1, 30)}
    counts["2026-09-30"] = {"2330": 100, "2603": 12, "MARKET": 500}
    sig = measure.signals(counts)["2026-09-30"]
    assert "MARKET" not in sig["top"] and sig["market"] == {"MARKET"}
    assert sig["spike"] == {"2603"}               # 2330 is famous, not spiking
    assert sig["top"] == {"2330", "2603"}


def test_stance_groups():
    t = lambda b, r: {"bullish": b, "bearish": r, "neutral": 0, "mixed": 0, "labelled": b + r}
    assert measure.stance_group(t(8, 2)) == "Bullish"
    assert measure.stance_group(t(2, 8)) == "Bearish"
    assert measure.stance_group(t(5, 4)) == "Split"
    assert measure.stance_group(t(2, 1)) is None


# --- backtest ------------------------------------------------------------------------

def test_entry_is_the_next_trading_day_and_returns_run_open_to_close():
    days = ["2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06"]
    assert backtest.entry_index(days, "2026-10-02") == 2        # Friday's talk → Monday
    assert backtest.entry_index(days, "2026-10-03") == 2        # weekend talk → Monday
    px = {"2026-10-05": (100.0, 110.0), "2026-10-06": (110.0, 121.0)}
    assert backtest.forward_return(px, days, 2, 1) == pytest.approx(0.10)
    assert backtest.forward_return(px, days, 2, 2) == pytest.approx(0.21)
    assert backtest.forward_return(px, days, 2, 5) is None


def test_excess_return_and_market_raw_return():
    bench = {"2026-10-01": (100.0, 100.0), "2026-10-02": (100.0, 101.0)}
    stock = {"2026-10-02": (50.0, 55.0)}
    sig = {"2026-10-01": {"top": {"2330"}, "spike": set(), "market": {"MARKET"}}}
    rows = backtest.run(sig, {}, lambda c: bench if c == "MARKET" else stock, bench)
    by = {r["kind"]: r for r in rows}
    assert by["Top Mentioned"]["1D"] == pytest.approx(0.10 - 0.01)
    assert by["Market"]["1D"] == pytest.approx(0.01)


def test_an_overseas_instrument_is_judged_on_its_own_market_and_days():
    tw = {"2026-10-01": (100.0, 100.0), "2026-10-02": (100.0, 101.0)}
    us = {"2026-10-01": (10.0, 10.0), "2026-10-02": (10.0, 10.2)}       # US Oct 2 opens after Taipei's Oct 2
    nvda = {"2026-10-02": (200.0, 210.0)}
    sig = {"2026-10-01": {"top": {"NVDA"}, "spike": set()}}
    rows = backtest.run(sig, {}, lambda c: nvda, tw, lambda c: us)
    [row] = rows
    assert row["entry"] == "2026-10-02"
    assert row["1D"] == pytest.approx(0.05 - 0.02)
    assert config.benchmark_of("NASDAQ") == "SP500_TR" and config.benchmark_of("KRX") == "KOSPI"
    assert config.benchmark_of("TPEx") == config.BENCHMARK


def test_returns_from_entry_are_plain_returns_on_the_given_days():
    days = ["2026-10-01", "2026-10-02", "2026-10-05"]
    px = {"2026-10-02": (100.0, 104.0), "2026-10-05": (104.0, 103.0)}
    got = backtest.returns_from(px, days, "2026-10-01")
    assert got["1D"] == pytest.approx(0.04) and got["1W"] is None
    assert backtest.returns_from(px, days, "2026-10-05") == {h: None for h in config.HORIZONS}


def test_summary_is_plain_returns_with_the_benchmark_over_the_same_days():
    bench = {"2026-10-01": (100.0, 100.0), "2026-10-02": (100.0, 101.0), "2026-10-05": (101.0, 100.0)}
    up, down = {"2026-10-02": (50.0, 55.0)}, {"2026-10-05": (50.0, 49.0)}
    sig = {"2026-10-01": {"top": {"UP"}, "spike": set()}, "2026-10-02": {"top": {"DOWN"}, "spike": set()}}
    rows = backtest.run(sig, {}, lambda c: up if c == "UP" else down, bench)
    assert rows[0]["raw"]["1D"] == pytest.approx(0.10) and rows[0]["bench"]["1D"] == pytest.approx(0.01)
    [s] = [s for s in backtest.summarise(rows) if s["kind"] == "Top Mentioned" and s["group"] == "All" and s["horizon"] == "1D"]
    assert s["n"] == 2
    assert s["mean"] == pytest.approx((0.10 - 0.02) / 2)                    # plain returns
    assert s["bench"] == pytest.approx((0.01 + (100 / 101 - 1)) / 2)      # TAIEX on the same days
    assert s["hit"] == 0.5                                                  # UP beat it, DOWN did not
