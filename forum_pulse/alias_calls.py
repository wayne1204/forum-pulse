"""Alias Calls: the model decides what an Ambiguous Alias means in one Comment
(三星 the Taiwan stock, Samsung, or nothing; 川寶 the stock or Trump), for the
hits the user has no rule for. The user's rules always win over a call."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from pydantic import BaseModel

from . import config
from .instruments import NOT, candidates_key
from .stance import QuotaExhausted, _api_caller, _cli_caller, _safe

SYSTEM = """You read comments from PTT Stock (批踢踢股票板), Taiwan's largest retail stock forum. Each item quotes one WORD from a comment that might name a listed company, and the choices it could mean. Decide what the word means IN THIS COMMENT.

- Pick an instrument's code when the comment is about that company or its stock: its price, earnings, products as a business, news, buying or selling it.
- Pick NOT when the word is used as an ordinary word (大量 "a lot", 統一 "unify", 全新 "brand new", 創意 "creativity"), a person or nickname (川寶 / 川普 is Donald Trump), a year (2008 金融海嘯, 夢迴2020, 2027 的 EPS), a place, an event, or anything other than these instruments.
- When a Taiwan and an overseas company share a name, read the context: 三星 next to 記憶體, HBM, 韓國, 手機 or Samsung's results is Samsung (Korea), not the Taiwan stock 5007 三星.
- Judge only the line marked >>; the other lines are context.

Return one choice for every item id you are given, using a code from that item's choices exactly as written."""


class Call(BaseModel):
    id: int
    choice: str


class Calls(BaseModel):
    items: list[Call]


_WHERE = {"TWSE": "Taiwan-listed", "TPEx": "Taiwan OTC", "NASDAQ": "US-listed", "NYSE": "US-listed",
          "KRX": "Korea-listed"}


def _pending(con) -> list[dict]:
    """Hits waiting in the Review Queue, newest Forum Day first."""
    cands: dict[str, set[str]] = {}
    for r in con.execute("SELECT alias, code FROM aliases"):
        cands.setdefault(r["alias"], set()).add(r["code"])
    rows = con.execute("""
        SELECT h.comment_id, h.alias, c.post_id, c.seq, c.tag, c.text, p.title FROM hits h
        JOIN comments c ON c.id=h.comment_id JOIN posts p USING(post_id)
        WHERE h.code IS NULL ORDER BY c.day DESC, c.id DESC""").fetchall()
    return [dict(r, candidates=cands[r["alias"]]) for r in rows if len(cands.get(r["alias"], ())) > 1]


def _context(con, h: dict, inst: dict[str, dict]) -> str:
    def choice(code):
        if code == NOT:
            return "NOT (names none of these here)"
        if code == config.MARKET:
            return f"{code} (大盤, the Taiwan market as a whole)"
        i = inst.get(code, {})
        return f"{code} {i.get('name', '')} ({_WHERE.get(i.get('exchange'), 'listed')})"

    lines = [f"Word: {h['alias']}",
             "Choices: " + " | ".join(choice(c) for c in sorted(h["candidates"] | {NOT})),
             f"[post] {h['title']}"]
    if h["seq"] == 0:
        # A post body can be long: show the stretch around the word.
        text = h["text"]
        at = max(text.find(h["alias"]), 0)
        lines.append(f">> post body: …{text[max(at - 200, 0):at + 200]}…")
    else:
        before = con.execute("SELECT user, tag, text FROM comments WHERE post_id=? AND seq<? AND seq>0 "
                             "ORDER BY seq DESC LIMIT 2", (h["post_id"], h["seq"])).fetchall()
        lines += [f"   context {b['tag']}: {b['text'][:120]}" for b in reversed(before)]
        lines.append(f">> {h['tag']}: {h['text'][:200]}")
    return "\n".join(lines)


def decide(con, caller=None, log=print) -> dict:
    """Make Alias Calls for queued hits, at most ALIAS_CALLS_MAX_PER_RUN, and
    resolve those hits. `quota` is True when the usage limit stopped the run."""
    pending = _pending(con)
    if not pending:
        log("  aliases: nothing to decide")
        return {"decided": 0, "pending": 0, "quota": False}
    backend = config.LLM_BACKEND
    if caller is None:
        try:
            caller = (_cli_caller if backend == "claude-cli" else _api_caller)(SYSTEM, Calls)
        except Exception as e:
            log(f"  aliases: {backend} unavailable ({type(e).__name__}: {e}); {len(pending)} hits stay queued")
            return {"decided": 0, "pending": len(pending), "quota": False}
    run_now = pending[:config.ALIAS_CALLS_MAX_PER_RUN]
    log(f"  aliases: {len(pending)} Ambiguous hits queued; deciding {len(run_now)}")
    inst = {r["code"]: dict(r) for r in con.execute("SELECT code, name, exchange FROM instruments")}
    prompts = []
    for i in range(0, len(run_now), config.LLM_ITEMS_PER_REQUEST):
        batch = run_now[i:i + config.LLM_ITEMS_PER_REQUEST]
        prompts.append((batch, "\n\n".join(f"### item {k}\n{_context(con, h, inst)}" for k, h in enumerate(batch))))

    def run(job):
        batch, prompt = job
        items, cost = caller(prompt)
        # The model sometimes answers with the whole choice ("005930.KS 三星電子
        # (Korea-listed)") rather than its code; the code is the first word.
        return job, {c.id: (c.choice.split() or [""])[0] for c in items}, cost

    decided, quota = 0, None
    with ThreadPoolExecutor(config.LLM_WORKERS) as pool:
        for i in range(0, len(prompts), config.LLM_WORKERS):
            results = list(pool.map(_safe(run, log), prompts[i:i + config.LLM_WORKERS]))
            quota = next((g for _, g, _ in results if isinstance(g, QuotaExhausted)), None)
            for job, got, _ in results:
                if isinstance(got, QuotaExhausted):
                    continue
                batch = job[0]
                for k, h in enumerate(batch):
                    code = got.get(k)
                    # NOT is always a choice: 力士 inside 勞力士 names neither candidate.
                    if code not in h["candidates"] | {NOT}:   # unanswered, or not one of the choices
                        continue
                    con.execute("INSERT OR REPLACE INTO alias_calls VALUES(?,?,?,?)",
                                (h["comment_id"], h["alias"], code, candidates_key(h["candidates"])))
                    con.execute("UPDATE hits SET code=? WHERE comment_id=? AND alias=? AND code IS NULL",
                                (code, h["comment_id"], h["alias"]))
                    decided += 1
            con.commit()
            if quota:
                log(f"  aliases: usage limit reached ({quota}); stopping, the next run continues")
                break
            if (i // config.LLM_WORKERS) % 10 == 0:
                log(f"  aliases: {decided}/{len(run_now)} decided")
    left = len(pending) - decided
    log(f"  aliases: decided {decided}, {left} left for later runs")
    return {"decided": decided, "pending": left, "quota": bool(quota)}
