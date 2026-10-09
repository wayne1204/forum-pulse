import re

import pytest

from conftest import DAY, LISTED, SLANG, add_post
from forum_pulse import alias_calls, config, instruments, measure, pipeline
from forum_pulse.instruments import NOT, Matcher, resolve
from forum_pulse.stance import QuotaExhausted

PID = "M.1790920910.A.717"
quiet = lambda *_: None

OVERSEAS = [
    {"code": "000660.KS", "name": "SK海力士", "exchange": "KRX", "yf_symbol": "000660.KS",
     "aliases": ["海力士", "力士"]},
    {"code": "AAPL", "name": "蘋果", "exchange": "NASDAQ", "yf_symbol": "AAPL", "aliases": ["Apple", "蘋果"]},
    {"code": "005930.KS", "name": "三星電子", "exchange": "KRX", "yf_symbol": "005930.KS", "aliases": ["三星"]},
]
LISTED_TW = LISTED + [
    {"code": "4923", "name": "力士", "kind": "stock", "exchange": "TPEx", "yf_symbol": "4923.TWO"},
    {"code": "5007", "name": "三星", "kind": "stock", "exchange": "TWSE", "yf_symbol": "5007.TW"},
]
SLANG_OVERSEAS = {**SLANG, "maybe_nothing": SLANG["maybe_nothing"] + ["三星", "蘋果"], "overseas": OVERSEAS}


@pytest.fixture
def abroad(world):
    instruments.refresh(world, LISTED_TW, SLANG_OVERSEAS)
    world.commit()
    return world


# --- Overseas Instruments -----------------------------------------------------------

def test_overseas_instruments_join_the_listing(abroad):
    row = abroad.execute("SELECT name, kind, exchange, yf_symbol FROM instruments WHERE code='000660.KS'").fetchone()
    assert tuple(row) == ("SK海力士", "stock", "KRX", "000660.KS")


def test_a_longer_overseas_name_wins_over_the_taiwan_name_inside_it(abroad):
    m = Matcher.from_db(abroad)
    assert m.find("海力士 HBM 賣爆") == {"海力士": {"000660.KS"}}
    assert m.find("力士 漲停") == {"力士": {"4923", "000660.KS"}}      # alone: Ambiguous


def test_a_shared_name_keeps_every_candidate_and_maybe_nothing_applies_to_overseas(abroad):
    m = Matcher.from_db(abroad)
    assert m.find("三星 記憶體 漲價") == {"三星": {"5007", "005930.KS", NOT}}
    assert m.find("蘋果 好吃") == {"蘋果": {"AAPL", NOT}}
    assert m.find("Apple 財報") == {"Apple": {"AAPL"}}


def test_a_slang_entry_decides_for_itself_even_if_also_maybe_nothing(world):
    slang = {**SLANG_OVERSEAS, "slang": {**SLANG["slang"], "統一": "1216"}}
    instruments.refresh(world, LISTED_TW, slang)
    assert Matcher.from_db(world).find("統一 漲停") == {"統一": {"1216"}}


def test_the_shipped_overseas_list_is_well_formed():
    slang = instruments.load_slang()
    codes = [r["code"] for r in slang["overseas"]]
    assert len(codes) == len(set(codes))
    assert all(r["exchange"] in config.EXCHANGE_MARKET and r["yf_symbol"] for r in slang["overseas"])
    assert "川寶" in slang["maybe_nothing"]


# --- resolving with Alias Calls -------------------------------------------------------

def test_a_rule_beats_an_alias_call_and_a_stale_call_is_ignored():
    c = {"5007", "005930.KS", NOT}
    calls = {(1, "三星"): ("005930.KS", "005930.KS,5007,NOT")}
    assert resolve("三星", c, "三星 HBM", 1, {}, calls) == "005930.KS"
    rules = {"三星": [{"scope": "always", "code": NOT, "context": "", "comment_id": 0}]}
    assert resolve("三星", c, "三星 HBM", 1, rules, calls) == NOT
    assert resolve("三星", c | {"9999"}, "三星 HBM", 1, {}, calls) is None     # candidates changed: ask again
    assert resolve("三星", c, "三星 HBM", 2, {}, calls) is None                 # another Comment


# --- deciding ---------------------------------------------------------------------------

@pytest.fixture
def queued(abroad):
    """u1 means Samsung, u2 the Taiwan 三星, u3 Trump-free 統一 the verb."""
    add_post(abroad, PID, "[新聞] 記憶體漲價", "host", DAY,
             [("u1", "三星 HBM 又出包"), ("u2", "三星 5007 漲停"), ("u3", "統一 意見吧")])
    measure.match_comments(abroad, log=quiet)
    abroad.commit()
    return abroad


def caller_choosing(by_word, prompts=None):
    def call(prompt):
        if prompts is not None:
            prompts.append(prompt)
        items = re.split(r"^### item (\d+)$", prompt, flags=re.M)[1:]
        out = []
        for i, body in zip(items[::2], items[1::2]):
            line = re.search(r"^>> .*$", body, re.M).group(0)
            choice = next((v for k, v in by_word.items() if k in line), None)
            if choice:
                out.append(alias_calls.Call(id=int(i), choice=choice))
        return out, 0.0
    return call


def _hits(con):
    return {(r["text"], r["alias"]): r["code"] for r in con.execute(
        "SELECT c.text, h.alias, h.code FROM hits h JOIN comments c ON c.id=h.comment_id WHERE c.seq>0")}


def test_pending_is_queued_hits_with_their_candidates(queued):
    got = {(h["text"], h["alias"]): h["candidates"] for h in alias_calls._pending(queued)}
    assert got == {("三星 HBM 又出包", "三星"): {"5007", "005930.KS", NOT},
                   ("三星 5007 漲停", "三星"): {"5007", "005930.KS", NOT},
                   ("統一 意見吧", "統一"): {"1216", NOT}}


def test_calls_resolve_the_hits_and_show_the_choices(queued):
    prompts = []
    out = alias_calls.decide(queued, caller=caller_choosing(
        {"HBM": "005930.KS", "5007 漲停": "5007", "意見": NOT}, prompts), log=quiet)
    assert out == {"decided": 3, "pending": 0, "quota": False}
    hits = _hits(queued)
    assert hits[("三星 HBM 又出包", "三星")] == "005930.KS"
    assert hits[("三星 5007 漲停", "三星")] == "5007"
    assert hits[("統一 意見吧", "統一")] == NOT
    assert "005930.KS 三星電子 (Korea-listed)" in prompts[0] and "NOT (names none" in prompts[0]
    assert queued.execute("SELECT COUNT(*) FROM alias_calls").fetchone()[0] == 3


def test_an_answer_quoting_the_whole_choice_counts_as_its_code(queued):
    out = alias_calls.decide(queued, caller=caller_choosing(
        {"HBM": "005930.KS 三星電子 (Korea-listed)", "5007 漲停": "5007", "意見": "NOT (names none of these here)"}),
        log=quiet)
    assert out["decided"] == 3
    assert _hits(queued)[("三星 HBM 又出包", "三星")] == "005930.KS"


def test_not_is_a_choice_even_when_the_alias_has_no_not(abroad):
    add_post(abroad, PID, "[閒聊] 盤中", "host", DAY, [("u1", "買鑽石不如買勞力士")])
    measure.match_comments(abroad, log=quiet)
    prompts = []
    out = alias_calls.decide(abroad, caller=caller_choosing({"勞力士": NOT}, prompts), log=quiet)
    assert out["decided"] == 1 and "NOT (names none" in prompts[0]
    assert _hits(abroad)[("買鑽石不如買勞力士", "力士")] == NOT


def test_an_answer_outside_the_choices_leaves_the_hit_queued(queued):
    out = alias_calls.decide(queued, caller=caller_choosing({"HBM": "2330"}), log=quiet)
    assert out["decided"] == 0 and out["pending"] == 3


def test_the_usage_limit_stops_the_run(queued):
    def call(prompt):
        raise QuotaExhausted("resets 4am")
    logs = []
    out = alias_calls.decide(queued, caller=call, log=logs.append)
    assert out == {"decided": 0, "pending": 3, "quota": True}
    assert any("usage limit reached (resets 4am)" in l for l in logs)


def test_calls_survive_a_rebuild_and_a_user_rule_overrides_them(queued):
    alias_calls.decide(queued, caller=caller_choosing({"HBM": "005930.KS", "5007 漲停": "5007", "意見": NOT}),
                       log=quiet)
    queued.execute("INSERT INTO alias_rules(alias, scope, code) VALUES('三星', 'context', '5007')")
    queued.execute("UPDATE alias_rules SET context='漲停' WHERE alias='三星'")
    queued.commit()
    pipeline.rebuild(log=quiet)
    assert {tuple(r) for r in queued.execute("SELECT user, code FROM mentions WHERE user != 'host'")} == \
        {("u1", "005930.KS"), ("u2", "5007")}


def test_nothing_queued(abroad):
    assert alias_calls.decide(abroad, caller=None, log=quiet) == {"decided": 0, "pending": 0, "quota": False}
