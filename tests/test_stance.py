import json
import re
import sys
from datetime import timedelta
from types import SimpleNamespace

import pytest

from conftest import DAY, add_post
from forum_pulse import config, measure, stance
from forum_pulse.stance import Label, Labels

PID = "M.1790920910.A.717"


def _measure(con):
    measure.match_comments(con, log=lambda *_: None)
    measure.build_mentions(con)
    measure.author_stances(con)
    con.commit()


@pytest.fixture
def forum(world):
    """bull wrote a 標的 Post (an Author Stance); u1 names 2330, u2 the Market."""
    add_post(world, PID, "[標的] 2330 台積電 多", "bull", DAY, [("u1", "GG 噴"), ("u2", "大盤要崩")])
    _measure(world)
    return world


def caller_saying(stance_, prompts=None, cost=0.0):
    def call(prompt):
        if prompts is not None:
            prompts.append(prompt)
        n = len(re.findall(r"^### item \d+$", prompt, re.M))
        return [Label(id=i, stance=stance_) for i in range(n)], cost
    return call


def _stances(con):
    return {r["user"]: (r["stance"], r["source"], r["model_stance"])
            for r in con.execute("SELECT * FROM stances")}


def test_pending_is_signal_and_market_mentions_without_a_current_label(forum):
    got = {(m["user"], m["code"]) for m in stance._pending(forum)}
    assert got == {("bull", "2330"), ("u1", "2330"), ("u2", "MARKET")}


def test_days_before_label_from_are_never_pending(forum, monkeypatch):
    monkeypatch.setattr(config, "LLM_LABEL_FROM", (DAY + timedelta(days=1)).date())
    assert stance._pending(forum) == []


def test_the_model_labels_mentions_but_never_overrides_an_author_stance(forum):
    prompts = []
    out = stance.label(forum, caller=caller_saying("bearish", prompts), log=lambda *_: None)
    assert out == {"labelled": 3, "pending": 0, "cost": 0.0}
    assert _stances(forum) == {"bull": ("bullish", "author", "bearish"),
                               "u1": ("bearish", "model", None),
                               "u2": ("bearish", "model", None)}
    prompt = "\n".join(prompts)
    assert "Instrument: 2330 台積電" in prompt
    assert "大盤 / the whole Taiwan market" in prompt
    assert ">> commenter (post body)" in prompt
    assert "context 推 u1: GG 噴" in prompt           # the push before u2's own
    assert stance.label(forum, caller=caller_saying("bearish"), log=lambda *_: None)["labelled"] == 0


def test_a_mention_that_grew_is_labelled_again(forum):
    stance.label(forum, caller=caller_saying("bearish"), log=lambda *_: None)
    add_post(forum, PID, "[標的] 2330 台積電 多", "bull", DAY,
             [("u1", "GG 噴"), ("u2", "大盤要崩"), ("u1", "台積電 還要噴")])
    _measure(forum)
    assert [(m["user"], m["n_comments"]) for m in stance._pending(forum)] == [("u1", 2)]
    stance.label(forum, caller=caller_saying("bullish"), log=lambda *_: None)
    assert _stances(forum)["u1"] == ("bullish", "model", None)


def test_the_cli_backend_stops_at_its_per_run_cap(forum, monkeypatch):
    monkeypatch.setattr(config, "LLM_BACKEND", "claude-cli")
    monkeypatch.setattr(config, "LLM_CLI_MAX_PER_RUN", 1)
    out = stance.label(forum, caller=caller_saying("neutral"), log=lambda *_: None)
    assert out == {"labelled": 1, "pending": 2, "cost": 0.0}


def test_the_api_backend_stops_at_its_budget(forum, monkeypatch):
    monkeypatch.setattr(config, "LLM_BACKEND", "api")
    monkeypatch.setattr(config, "LLM_ITEMS_PER_REQUEST", 1)
    monkeypatch.setattr(config, "LLM_WORKERS", 1)
    out = stance.label(forum, caller=caller_saying("neutral", cost=0.5), budget=0.75, log=lambda *_: None)
    assert out == {"labelled": 2, "pending": 1, "cost": 1.0}


def test_failing_requests_lose_nothing_and_end_the_run(forum, monkeypatch):
    monkeypatch.setattr(config, "LLM_ITEMS_PER_REQUEST", 1)
    monkeypatch.setattr(config, "LLM_WORKERS", 1)
    logs = []

    def broken(prompt):
        raise RuntimeError("quota")
    out = stance.label(forum, caller=broken, log=logs.append)
    assert out == {"labelled": 0, "pending": 3, "cost": 0.0}
    assert _stances(forum)["bull"] == ("bullish", "author", None)
    assert any("every request is failing" in m for m in logs)


def test_nothing_to_label(world):
    assert stance.label(world, caller=caller_saying("bullish"), log=lambda *_: None) == \
        {"labelled": 0, "pending": 0, "cost": 0.0}


def test_a_missing_backend_leaves_mentions_for_later(forum, monkeypatch):
    monkeypatch.setattr(config, "LLM_BACKEND", "claude-cli")
    monkeypatch.setattr(stance.shutil, "which", lambda _: None)
    monkeypatch.setattr(stance.os.path, "expanduser", lambda _: "/nonexistent/claude")
    logs = []
    assert stance.label(forum, log=logs.append) == {"labelled": 0, "pending": 3, "cost": 0.0}
    assert "unavailable" in logs[-1]


# --- the two callers ------------------------------------------------------------------

@pytest.fixture
def cli(monkeypatch):
    """`claude -p` replaced by a canned reply; returns (set_reply, calls)."""
    monkeypatch.setattr(stance.shutil, "which", lambda _: sys.executable)
    calls, reply = [], {}

    def run(args, **kw):
        calls.append((args, kw))
        return SimpleNamespace(**reply)
    monkeypatch.setattr(stance.subprocess, "run", run)

    def set_reply(returncode=0, stdout="", stderr=""):
        reply.update(returncode=returncode, stdout=stdout, stderr=stderr)
    return set_reply, calls


def test_cli_caller_asks_for_structured_labels(cli):
    set_reply, calls = cli
    set_reply(stdout=json.dumps({"structured_output": {"items": [{"id": 0, "stance": "mixed"}]}}))
    items, cost = stance._cli_caller()("### item 0\n...")
    assert items == [Label(id=0, stance="mixed")] and cost == 0.0
    args, kw = calls[0]
    assert "--json-schema" in args and kw["input"] == "### item 0\n..."
    assert kw["env"]["MAX_THINKING_TOKENS"] == "0" and kw["env"]["DISABLE_PROMPT_CACHING"] == "1"


@pytest.mark.parametrize("reply, msg", [
    ({"returncode": 1, "stderr": "rate limited"}, "rate limited"),
    ({"stdout": json.dumps({"is_error": True, "result": "bad schema"})}, "bad schema"),
])
def test_cli_caller_raises_on_errors(cli, reply, msg):
    set_reply, _ = cli
    set_reply(**reply)
    with pytest.raises(RuntimeError, match=msg):
        stance._cli_caller()("x")


def test_api_caller_prices_each_request(monkeypatch):
    parsed = Labels(items=[Label(id=0, stance="bullish")])
    client = SimpleNamespace(
        models=SimpleNamespace(retrieve=lambda model: None),
        messages=SimpleNamespace(parse=lambda **kw: SimpleNamespace(
            usage=SimpleNamespace(input_tokens=1000, output_tokens=100), parsed_output=parsed)))
    monkeypatch.setitem(sys.modules, "anthropic", SimpleNamespace(Anthropic=lambda: client))
    items, cost = stance._api_caller()("x")
    assert items == parsed.items
    assert cost == pytest.approx(1000 * config.LLM_PRICE_IN + 100 * config.LLM_PRICE_OUT)
