"""Labelling each Mention's Stance with Claude, through Claude Code headless
(subscription, the default) or the API. Every Mention is labelled, not only
Signals: the Day page shows each comment's Stance."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import Literal

from pydantic import BaseModel

from . import config

SYSTEM = """You read comments from PTT Stock (批踢踢股票板), Taiwan's largest retail stock forum, and judge each commenter's STANCE on one named instrument.

Stance is about direction, not mood:
- bullish: expects it to rise, is buying/holding long, cheering it up (噴, 起飛, 歐印, 買爆, 抱緊, 進場, 加碼, 多單)
- bearish: expects it to fall, is selling/shorting, warning others off (崩, 倒, 逃命, 出清, 放空, 空單, 畢業, 住套房 said as warning, 韭菜 being harvested)
- neutral: names it without a directional view (news, questions, facts, jokes with no direction)
- mixed: clearly holds both views, or the direction cannot be told apart

Read sarcasm the way PTT users mean it: "台積電要崩了 笑死 我歐印空單" is bearish even though it is gleeful; "又是我買就跌" by someone holding is usually still bullish-holding unless they say they sold. A 噓 tag is a reaction to the post, not to the instrument. The context lines are other users' comments shown only so replies make sense; judge ONLY the lines marked as the commenter's own.

Return one label for every item id you are given."""

Stance = Literal["bullish", "bearish", "neutral", "mixed"]


class Label(BaseModel):
    id: int
    stance: Stance


class Labels(BaseModel):
    items: list[Label]


def _pending(con) -> list[dict]:
    """Mentions without a current label, newest Forum Day first."""
    rows = con.execute("""
        SELECT m.forum, m.day, m.user, m.code, m.n_comments, s.source, s.n_comments s_n, s.model_stance
        FROM mentions m LEFT JOIN stances s USING(forum, day, user, code)
        ORDER BY m.day DESC""").fetchall()
    out = []
    since = config.LLM_LABEL_FROM.isoformat()
    for r in rows:
        if r["day"] < since:
            continue
        if r["source"] == "author" and r["model_stance"] is not None:
            continue
        if r["source"] == "model" and r["s_n"] == r["n_comments"]:
            continue
        out.append(dict(r))
    return out


def _context(con, m: dict, names: dict[str, str]) -> str:
    """What the model sees for one Mention: each Post the user spoke in, its
    title, and the few pushes before each of the user's own lines."""
    own = con.execute(f"""
        SELECT c.post_id, c.seq, c.tag, c.text, p.title FROM comments c
        JOIN hits h ON h.comment_id=c.id JOIN posts p USING(post_id)
        WHERE c.forum=? AND c.day=? AND c.user=? AND h.code=?
        ORDER BY c.at LIMIT 8""", (m["forum"], m["day"], m["user"], m["code"])).fetchall()
    lines = []
    by_post = defaultdict(list)
    for r in own:
        by_post[(r["post_id"], r["title"])].append(r)
    for (pid, title), rs in by_post.items():
        lines.append(f"  [post] {title}")
        for r in rs:
            if r["seq"] == 0:
                lines.append(f"  >> commenter (post body): {r['text'][:600]}")
                continue
            before = con.execute("SELECT user, tag, text FROM comments WHERE post_id=? AND seq<? AND seq>0 "
                                 "ORDER BY seq DESC LIMIT ?", (pid, r["seq"], config.LLM_CONTEXT_PUSHES)).fetchall()
            for b in reversed(before):
                lines.append(f"     context {b['tag']} {b['user']}: {b['text'][:120]}")
            lines.append(f"  >> commenter {r['tag']}: {r['text'][:200]}")
    name = names.get(m["code"], m["code"])
    label = "大盤 / the whole Taiwan market (TAIEX, 台指期)" if m["code"] == config.MARKET else f"{m['code']} {name}"
    return f"Instrument: {label}\n" + "\n".join(lines)


class QuotaExhausted(Exception):
    """The Claude subscription's usage limit is reached; stop the run."""


def _api_caller(system: str = SYSTEM, out: type[BaseModel] = Labels):
    """Anthropic API: billed per token. Returns call(prompt) -> (items, USD),
    `items` being the `out` model's list of answers."""
    import anthropic
    client = anthropic.Anthropic()
    client.models.retrieve(config.LLM_MODEL)     # fail fast without credentials

    def call(prompt):
        resp = client.messages.parse(
            model=config.LLM_MODEL, max_tokens=4000, system=system,
            messages=[{"role": "user", "content": prompt}], output_format=out)
        cost = resp.usage.input_tokens * config.LLM_PRICE_IN + resp.usage.output_tokens * config.LLM_PRICE_OUT
        return ([] if resp.parsed_output is None else resp.parsed_output.items), cost
    return call


def _cli_caller(system: str = SYSTEM, out: type[BaseModel] = Labels):
    """Claude Code headless (`claude -p`): runs on the user's Claude
    subscription, so there is no API bill — only subscription usage."""
    schema = json.dumps(out.model_json_schema())
    exe = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
    if not os.path.exists(exe):
        raise FileNotFoundError("claude CLI not found")
    # Thinking was ~87% of output tokens and made labels worse against Author
    # Stances (77% vs 95% agreement); no batch repeats a prompt, so a cache
    # write is paid for and never read. Together ~4x less usage per Mention.
    env = {**os.environ, "MAX_THINKING_TOKENS": "0", "DISABLE_PROMPT_CACHING": "1"}

    def call(prompt):
        res = subprocess.run(
            [exe, "-p", "--model", config.LLM_CLI_MODEL, "--tools", "", "--no-session-persistence",
             "--system-prompt", system, "--output-format", "json", "--json-schema", schema],
            input=prompt, capture_output=True, text=True, timeout=300, cwd=config.DATA_DIR, env=env)
        if res.returncode != 0 and '"api_error_status":429' not in res.stdout:
            raise RuntimeError((res.stderr or res.stdout)[-300:])
        d = json.loads(res.stdout or "{}")
        if d.get("api_error_status") == 429:
            raise QuotaExhausted(str(d.get("result"))[:200])
        if d.get("is_error") or not d.get("structured_output"):
            raise RuntimeError(str(d.get("result"))[:300])
        return out.model_validate(d["structured_output"]).items, 0.0
    return call


def label(con, caller=None, budget: float | None = None, log=print) -> dict:
    """Label pending Mentions. With the API backend a run stops at `budget`
    USD; with the CLI backend, after LLM_CLI_MAX_PER_RUN Mentions."""
    backend = config.LLM_BACKEND
    budget = config.LLM_BUDGET_USD_PER_RUN if budget is None else budget
    pending = _pending(con)
    if not pending:
        log("  stance: nothing to label")
        return {"labelled": 0, "pending": 0, "cost": 0.0}
    names = {r["code"]: r["name"] for r in con.execute("SELECT code, name FROM instruments")}
    if caller is None:
        try:
            caller = _cli_caller() if backend == "claude-cli" else _api_caller()
        except Exception as e:
            log(f"  stance: {backend} unavailable ({type(e).__name__}: {e}); {len(pending)} Mentions left unlabelled")
            return {"labelled": 0, "pending": len(pending), "cost": 0.0}
    if backend == "claude-cli":
        pending_run = pending[:config.LLM_CLI_MAX_PER_RUN]
        log(f"  stance: {len(pending)} Mentions pending; labelling {len(pending_run)} via claude -p (subscription)")
    else:
        pending_run = pending
        log(f"  stance: {len(pending)} Mentions pending via API; this run's budget ${budget:.2f}")
    batches = [pending_run[i:i + config.LLM_ITEMS_PER_REQUEST]
               for i in range(0, len(pending_run), config.LLM_ITEMS_PER_REQUEST)]

    # sqlite connections stay on their own thread
    local = threading.local()

    def con_local():
        if getattr(local, "con", None) is None:
            local.con = sqlite3.connect(config.DB_PATH, timeout=60)
            local.con.row_factory = sqlite3.Row
        return local.con

    def run(batch):
        prompt = "\n\n".join(f"### item {i}\n{_context(con_local(), m, names)}" for i, m in enumerate(batch))
        items, cost = caller(prompt)
        return batch, {l.id: l.stance for l in items}, cost

    spent, labelled, failed = 0.0, 0, 0
    with ThreadPoolExecutor(config.LLM_WORKERS) as pool:
        for i in range(0, len(batches), config.LLM_WORKERS):
            if spent >= budget:
                break
            wave = batches[i:i + config.LLM_WORKERS]
            results = list(pool.map(_safe(run, log), wave))
            quota = next((g for _, g, _ in results if isinstance(g, QuotaExhausted)), None)
            results = [r for r in results if not isinstance(r[1], QuotaExhausted)]
            for batch, got, cost in results:
                spent += cost
                failed += not got
                for idx, m in enumerate(batch):
                    s = got.get(idx)
                    if s is None:
                        continue
                    if m["source"] == "author":
                        con.execute("UPDATE stances SET model_stance=? WHERE forum=? AND day=? AND user=? AND code=?",
                                    (s, m["forum"], m["day"], m["user"], m["code"]))
                    else:
                        con.execute("""INSERT INTO stances(forum, day, user, code, stance, source, n_comments)
                                       VALUES(?,?,?,?,?,'model',?)
                                       ON CONFLICT(forum, day, user, code) DO UPDATE SET
                                         stance=excluded.stance, n_comments=excluded.n_comments""",
                                    (m["forum"], m["day"], m["user"], m["code"], s, m["n_comments"]))
                    labelled += 1
            con.commit()
            if quota:
                log(f"  stance: usage limit reached ({quota}); stopping, the next run continues")
                break
            if failed >= 3 * config.LLM_WORKERS and labelled == 0:
                log("  stance: every request is failing (quota or auth?); stopping this run")
                break
            if (i // config.LLM_WORKERS) % 10 == 0:
                log(f"  stance: {labelled}/{len(pending_run)} labelled" + (f", ${spent:.2f}" if spent else ""))
    left = len(pending) - labelled
    log(f"  stance: labelled {labelled}" + (f", ${spent:.2f} spent" if spent else "") + f", {left} left for later runs")
    return {"labelled": labelled, "pending": left, "cost": spent}


def _safe(fn, log):
    def wrapped(batch):
        try:
            return fn(batch)
        except QuotaExhausted as e:
            return batch, e, 0.0          # keep the wave's other results
        except Exception as e:      # one failed request must not lose the wave
            log(f"  stance: a request failed: {type(e).__name__}: {e}")
            return batch, {}, 0.0
    return wrapped
