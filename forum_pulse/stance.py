"""Labelling each Mention's Stance with Claude. Only Signals and the Market are
labelled — they are what the backtest and the Today page read — and one run
spends at most LLM_BUDGET_USD_PER_RUN."""
from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import Literal

from pydantic import BaseModel

from . import config
from .instruments import NOT
from .measure import daily_counts, signals

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
    """Mentions in a Signal (or of the Market) without a current label, newest
    Forum Day first."""
    sig = signals(daily_counts(con))
    wanted = {(d, c) for d, s in sig.items() for c in s["top"] | s["spike"]}
    rows = con.execute("""
        SELECT m.forum, m.day, m.user, m.code, m.n_comments, s.source, s.n_comments s_n, s.model_stance
        FROM mentions m LEFT JOIN stances s USING(forum, day, user, code)
        ORDER BY m.day DESC""").fetchall()
    out = []
    for r in rows:
        if r["code"] != config.MARKET and (r["day"], r["code"]) not in wanted:
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


def label(con, client=None, budget: float | None = None, log=print) -> dict:
    budget = config.LLM_BUDGET_USD_PER_RUN if budget is None else budget
    pending = _pending(con)
    if not pending:
        log("  stance: nothing to label")
        return {"labelled": 0, "pending": 0, "cost": 0.0}
    names = {r["code"]: r["name"] for r in con.execute("SELECT code, name FROM instruments")}
    if client is None:
        try:
            import anthropic
            client = anthropic.Anthropic()
            client.models.retrieve(config.LLM_MODEL)     # fail fast without credentials
        except Exception as e:
            log(f"  stance: no Claude credentials ({type(e).__name__}); {len(pending)} Mentions left unlabelled")
            return {"labelled": 0, "pending": len(pending), "cost": 0.0}

    batches = [pending[i:i + config.LLM_ITEMS_PER_REQUEST]
               for i in range(0, len(pending), config.LLM_ITEMS_PER_REQUEST)]
    est_chars = sum(len(_context(con, m, names)) for m in pending[:200]) / min(len(pending), 200)
    est = len(pending) * est_chars * 1.3 * config.LLM_PRICE_IN + len(pending) * 12 * config.LLM_PRICE_OUT
    log(f"  stance: {len(pending)} Mentions to label, est. ${est:.2f}; this run's budget ${budget:.2f}")

    def run(batch):
        prompt = "\n\n".join(f"### item {i}\n{_context(con_local(), m, names)}" for i, m in enumerate(batch))
        resp = client.messages.parse(
            model=config.LLM_MODEL, max_tokens=4000, system=SYSTEM,
            messages=[{"role": "user", "content": prompt}], output_format=Labels)
        cost = resp.usage.input_tokens * config.LLM_PRICE_IN + resp.usage.output_tokens * config.LLM_PRICE_OUT
        got = {} if resp.parsed_output is None else {l.id: l.stance for l in resp.parsed_output.items}
        return batch, got, cost

    # sqlite connections stay on their own thread
    import sqlite3, threading
    local = threading.local()

    def con_local():
        if getattr(local, "con", None) is None:
            local.con = sqlite3.connect(config.DB_PATH, timeout=60)
            local.con.row_factory = sqlite3.Row
        return local.con

    spent, labelled = 0.0, 0
    with ThreadPoolExecutor(config.LLM_WORKERS) as pool:
        for i in range(0, len(batches), config.LLM_WORKERS):
            if spent >= budget:
                break
            wave = batches[i:i + config.LLM_WORKERS]
            for batch, got, cost in pool.map(_safe(run, log), wave):
                spent += cost
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
            done = min(i + config.LLM_WORKERS, len(batches)) * config.LLM_ITEMS_PER_REQUEST
            if (i // config.LLM_WORKERS) % 10 == 0:
                log(f"  stance: ~{min(done, len(pending))}/{len(pending)} sent, ${spent:.2f} spent")
    left = len(pending) - labelled
    log(f"  stance: labelled {labelled}, ${spent:.2f} spent, {left} left for later runs")
    return {"labelled": labelled, "pending": left, "cost": spent}


def _safe(fn, log):
    def wrapped(batch):
        try:
            return fn(batch)
        except Exception as e:      # one failed request must not lose the wave
            log(f"  stance: a request failed: {type(e).__name__}: {e}")
            return batch, {}, 0.0
    return wrapped
