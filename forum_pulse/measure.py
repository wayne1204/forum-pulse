"""From Comments to Mentions to the day's numbers: Top Mentioned, Buzz Spikes
and Net Stance."""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, timedelta

from . import config
from .instruments import NOT, Matcher, load_calls, load_rules, resolve

_AUTHOR_STANCE = re.compile(r"(多|空)\s*$")


def match_comments(con, log=print) -> int:
    """Look for Aliases in every Comment not yet searched."""
    matcher = Matcher.from_db(con)
    rules, calls = load_rules(con), load_calls(con)
    todo = con.execute("SELECT c.id, c.text, p.title, c.seq FROM comments c "
                       "JOIN posts p USING(post_id) WHERE c.matched=0").fetchall()
    n = 0
    for r in todo:
        # A Post's title is part of what its author wrote.
        text = f"{r['title']}\n{r['text']}" if r["seq"] == 0 else r["text"]
        found = matcher.find(text)
        con.execute("DELETE FROM hits WHERE comment_id=?", (r["id"],))
        con.executemany("INSERT INTO hits VALUES(?,?,?)",
                        [(r["id"], a, resolve(a, c, text, r["id"], rules, calls)) for a, c in found.items()])
        con.execute("UPDATE comments SET matched=1 WHERE id=?", (r["id"],))
        n += 1
        if n % 50000 == 0:
            con.commit()
            log(f"  comments searched: {n}/{len(todo)}")
    return n


def reresolve(con) -> None:
    """Apply the user's current rules and the Alias Calls to every hit again,
    after a decision in the Review Queue. Aliases themselves are not searched
    for again."""
    rules, calls = load_rules(con), load_calls(con)
    cands: dict[str, set[str]] = defaultdict(set)
    for r in con.execute("SELECT alias, code FROM aliases"):
        cands[r["alias"]].add(r["code"])
    rows = con.execute("SELECT h.comment_id, h.alias, h.code, c.text, c.seq, p.title FROM hits h "
                       "JOIN comments c ON c.id=h.comment_id JOIN posts p USING(post_id)").fetchall()
    for r in rows:
        text = f"{r['title']}\n{r['text']}" if r["seq"] == 0 else r["text"]
        code = resolve(r["alias"], cands.get(r["alias"], set()), text, r["comment_id"], rules, calls)
        if code != r["code"]:
            con.execute("UPDATE hits SET code=? WHERE comment_id=? AND alias=?",
                        (code, r["comment_id"], r["alias"]))


def build_mentions(con) -> int:
    """One Mention per user, Instrument and Forum Day, from resolved hits."""
    con.execute("DELETE FROM mentions")
    con.execute(f"""
        INSERT INTO mentions(forum, day, user, code, n_comments)
        SELECT c.forum, c.day, c.user, h.code, COUNT(DISTINCT c.id)
        FROM hits h JOIN comments c ON c.id = h.comment_id
        WHERE h.code IS NOT NULL AND h.code != '{NOT}'
        GROUP BY c.forum, c.day, c.user, h.code""")
    return con.execute("SELECT COUNT(*) FROM mentions").fetchone()[0]


def author_stances(con) -> int:
    """標的 Posts that end in 多 or 空 give their author's Stance on the
    Instruments named in the title, overriding the model. Instruments named
    only in the body get none: they are comparisons and background."""
    # Start over, so a rule change never leaves a stale Author Stance behind:
    # one the model had labelled goes back to that label, the rest to the queue.
    con.execute("""UPDATE stances SET stance=model_stance, source='model', model_stance=NULL
                   WHERE source='author' AND model_stance IS NOT NULL""")
    con.execute("DELETE FROM stances WHERE source='author'")
    rows = con.execute(f"""
        SELECT p.title, c.forum, c.day, c.user, h.code, h.alias
        FROM posts p JOIN comments c ON c.post_id=p.post_id AND c.seq=0
        JOIN hits h ON h.comment_id=c.id
        WHERE p.post_type='標的' AND p.title NOT LIKE 'Re:%' AND p.title NOT LIKE 'Fw:%'
          AND h.code IS NOT NULL AND h.code != '{NOT}'""").fetchall()
    matcher = Matcher.from_db(con)
    in_title: dict[str, dict] = {}
    n = 0
    for r in rows:
        m = _AUTHOR_STANCE.search(r["title"])
        if not m:
            continue
        if r["title"] not in in_title:
            in_title[r["title"]] = matcher.find(r["title"])
        if r["alias"] not in in_title[r["title"]]:
            continue
        stance = "bullish" if m.group(1) == "多" else "bearish"
        mention = con.execute("SELECT n_comments FROM mentions WHERE forum=? AND day=? AND user=? AND code=?",
                              (r["forum"], r["day"], r["user"], r["code"])).fetchone()
        if not mention:
            continue
        con.execute("""INSERT INTO stances(forum, day, user, code, stance, source, n_comments)
                       VALUES(?,?,?,?,?,'author',?)
                       ON CONFLICT(forum, day, user, code) DO UPDATE SET
                         model_stance = CASE WHEN stances.source='model' THEN stances.stance
                                             ELSE stances.model_stance END,
                         stance=excluded.stance, source='author', n_comments=excluded.n_comments""",
                    (r["forum"], r["day"], r["user"], r["code"], stance, mention["n_comments"]))
        n += 1
    return n


# --- the day's numbers -------------------------------------------------------

def _weekdays_before(day: date, n: int) -> list[str]:
    out, d = [], day
    while len(out) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            out.append(d.isoformat())
    return out


def daily_counts(con) -> dict[str, dict[str, int]]:
    """{day: {code: Mentions}} across every Forum."""
    out: dict[str, dict[str, int]] = defaultdict(dict)
    for r in con.execute("SELECT day, code, COUNT(*) n FROM mentions GROUP BY day, code"):
        out[r["day"]][r["code"]] = r["n"]
    return out


def signals(counts: dict[str, dict[str, int]]) -> dict[str, dict[str, set[str]]]:
    """{day: {'top': codes, 'spike': codes}}. The Market is reported on its own
    and never takes a Top Mentioned place."""
    out = {}
    for day, by_code in counts.items():
        ranked = sorted(((n, c) for c, n in by_code.items() if c != config.MARKET), reverse=True)
        top = {c for _, c in ranked[:config.TOP_N]}
        prior = _weekdays_before(date.fromisoformat(day), config.SPIKE_LOOKBACK)
        spike = set()
        for c, n in by_code.items():
            if c == config.MARKET or n < config.SPIKE_MIN_MENTIONS:
                continue
            base = sum(counts.get(d, {}).get(c, 0) for d in prior) / len(prior)
            if n >= config.SPIKE_RATIO * base:
                spike.add(c)
        out[day] = {"top": top, "spike": spike,
                    "market": {config.MARKET} if config.MARKET in by_code else set()}
    return out


def stance_tally(con) -> dict[tuple[str, str], dict[str, int]]:
    """{(day, code): {bullish, bearish, neutral, mixed, labelled}} over Mentions
    whose label is current."""
    out: dict = defaultdict(lambda: {"bullish": 0, "bearish": 0, "neutral": 0, "mixed": 0, "labelled": 0})
    for r in con.execute("""
            SELECT m.day, m.code, s.stance, COUNT(*) n FROM mentions m
            JOIN stances s USING(forum, day, user, code)
            WHERE s.source='author' OR s.n_comments = m.n_comments
            GROUP BY m.day, m.code, s.stance"""):
        t = out[(r["day"], r["code"])]
        t[r["stance"]] += r["n"]
        t["labelled"] += r["n"]
    return out


def net_stance(t: dict[str, int]) -> float | None:
    decided = t["bullish"] + t["bearish"]
    return (t["bullish"] - t["bearish"]) / decided if decided else None


def stance_group(t: dict[str, int] | None) -> str | None:
    if not t or t["bullish"] + t["bearish"] < config.STANCE_GROUP_MIN_DECIDED:
        return None
    ns = net_stance(t)
    if ns >= config.STANCE_GROUP_CUTOFF:
        return "Bullish"
    if ns <= -config.STANCE_GROUP_CUTOFF:
        return "Bearish"
    return "Split"
