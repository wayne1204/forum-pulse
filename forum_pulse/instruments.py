"""Which Instruments exist, the Aliases that name them, and finding those
Aliases in a Comment."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import requests

from . import config

NOT = "NOT"
_ISIN = "https://isin.twse.com.tw/isin/C_public.jsp?strMode={mode}"
_KEEP_SECTIONS = {"股票": "stock", "ETF": "etf", "臺灣存託憑證": "stock",
                  "臺灣存託憑證(TDR)": "stock"}


def parse_isin(html: str, exchange: str) -> list[dict]:
    """Rows of the TWSE ISIN listing: section header rows, then one row per
    security whose first cell is '<code>　<name>'."""
    out = []
    kind = None
    for tr in html.split("<tr>")[1:]:
        cells = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        if len(cells) == 1:
            sec = re.sub(r"<[^>]+>", "", cells[0]).strip()
            kind = _KEEP_SECTIONS.get(sec)
            continue
        if kind is None or len(cells) < 4:
            continue
        first = re.sub(r"<[^>]+>", "", cells[0]).strip()
        parts = first.split("　", 1)
        if len(parts) != 2:
            continue
        # Some listed names carry a trailing * that forum users never type.
        code, name = parts[0].strip(), parts[1].strip().rstrip("*")
        suffix = ".TW" if exchange == "TWSE" else ".TWO"
        out.append({"code": code, "name": name, "kind": kind, "exchange": exchange,
                    "yf_symbol": code + suffix})
    return out


def fetch_listed() -> list[dict]:
    rows = []
    for mode, exchange in ((2, "TWSE"), (4, "TPEx")):
        r = requests.get(_ISIN.format(mode=mode), headers=config.HTTP_HEADERS,
                         timeout=config.HTTP_TIMEOUT * 2)
        r.raise_for_status()
        r.encoding = "big5hkscs"
        rows += parse_isin(r.text, exchange)
    return rows


def load_slang(path=None) -> dict:
    return json.loads(open(path or config.SLANG_PATH, encoding="utf-8").read())


def refresh(con, listed: list[dict] | None = None, slang: dict | None = None) -> int:
    """Rebuild the Instrument and Alias tables from the official listing plus
    the slang table. The user's alias_rules are left alone."""
    listed = listed if listed is not None else fetch_listed()
    slang = slang if slang is not None else load_slang()
    overseas = slang.get("overseas", [])
    con.execute("DELETE FROM instruments")
    con.execute("DELETE FROM aliases")
    con.executemany("INSERT OR REPLACE INTO instruments VALUES(:code,:name,:kind,:exchange,:yf_symbol)",
                    listed)
    con.executemany("INSERT OR REPLACE INTO instruments VALUES(:code,:name,'stock',:exchange,:yf_symbol)",
                    overseas)
    con.execute("INSERT INTO instruments VALUES(?, '大盤', 'market', '-', NULL)", (config.MARKET,))
    codes = {r["code"] for r in listed} | {r["code"] for r in overseas} | {config.MARKET}
    maybe_nothing = set(slang.get("maybe_nothing", []))
    rows = set()
    for r in listed:
        rows.add((r["code"], r["code"], "official"))
        rows.add((r["name"], r["code"], "official"))
    for alias, target in slang.get("slang", {}).items():
        targets = target if isinstance(target, list) else [target]
        # A slang entry speaks for its Alias: it replaces whatever the official
        # names said (台積 is 2330, not whichever company is officially 台積).
        rows = {x for x in rows if x[0] != alias}
        for t in targets:
            if t == NOT or t in codes:
                rows.add((alias, t, "slang"))
    # An overseas Alias joins whatever Taiwan Instrument shares it (三星 is
    # both 5007 and Samsung), leaving the model to tell them apart.
    for r in overseas:
        for alias in {r["name"], *r["aliases"]}:
            rows.add((alias, r["code"], "overseas"))
    # A slang entry already says whether its Alias may name nothing.
    for alias in (maybe_nothing & {x[0] for x in rows}) - set(slang.get("slang", {})):
        rows.add((alias, NOT, "official"))
    con.executemany("INSERT OR IGNORE INTO aliases VALUES(?,?,?)", sorted(rows))
    return len(listed)


# --- matching ---------------------------------------------------------------

def _trie_regex(words: list[str]) -> str:
    """One regex matching any of `words`, longest first, built as a trie so a
    few thousand Aliases still match quickly."""
    trie: dict = {}
    for w in words:
        node = trie
        for ch in w:
            node = node.setdefault(ch, {})
        node[""] = True

    def build(node) -> str:
        end = "" in node
        branches = [re.escape(ch) + build(sub) for ch, sub in sorted(node.items()) if ch]
        if not branches:
            return ""
        body = branches[0] if len(branches) == 1 else "(?:" + "|".join(branches) + ")"
        return f"(?:{body})?" if end else body

    return build(trie)


# A number that is a code: not part of a date, time, decimal, range or amount.
_CODE = re.compile(r"(?<![0-9A-Za-z.,/:~～$＄\-])(00\d{2,4}[A-Z]?|[1-9]\d{3}[A-Z]?)"
                   r"(?![0-9,/:%％~～\-]|\.\d)")
_AMOUNT_AFTER = re.compile(r"^\s*(?:元|塊|點|張|股|萬|億|年|月|天|附近|以上|以下|左右|支撐|壓力|價|多點|上下|起|檔)")
_PRICE_BEFORE = re.compile(r"(?:價|在|破|站上|跌破|守住|目標|回到|來到|到|至|買在|賣在|成本|均價|收)\s*$")
_LATIN = re.compile(r"[A-Za-z]")


@dataclass
class Matcher:
    """Finds Aliases in text. `candidates[alias]` is every code it might mean."""
    candidates: dict[str, set[str]]
    codes: set[str] = field(default_factory=set)

    def __post_init__(self):
        names = [a for a in self.candidates if not a.isdigit() and not re.fullmatch(r"00\d+[A-Z]?|\d{4}[A-Z]?", a)]
        cjk = [a for a in names if not _LATIN.search(a)]
        latin = [a for a in names if _LATIN.search(a)]
        self._cjk = re.compile(_trie_regex(cjk)) if cjk else None
        self._latin = (re.compile(r"(?<![A-Za-z0-9])(" + _trie_regex(latin) + r")(?![A-Za-z0-9])")
                       if latin else None)

    @classmethod
    def from_db(cls, con) -> "Matcher":
        cand: dict[str, set[str]] = {}
        for r in con.execute("SELECT alias, code FROM aliases"):
            cand.setdefault(r["alias"], set()).add(r["code"])
        codes = {r["code"] for r in con.execute("SELECT code FROM instruments")}
        return cls(cand, codes)

    def find(self, text: str) -> dict[str, set[str]]:
        """Aliases present in `text`, each with the codes it might mean."""
        found: dict[str, set[str]] = {}
        if self._cjk:
            for m in self._cjk.finditer(text):
                if m.group(0) in self.candidates:
                    found[m.group(0)] = set(self.candidates[m.group(0)])
        if self._latin:
            for m in self._latin.finditer(text):
                a = m.group(1)
                if a in self.candidates:
                    found[a] = set(self.candidates[a])
        named = {c for cs in found.values() for c in cs}
        for m in _CODE.finditer(text):
            code = m.group(1)
            if code not in self.candidates:
                continue
            if _AMOUNT_AFTER.match(text[m.end():]) or _PRICE_BEFORE.search(text[:m.start()]):
                continue
            cands = self.candidates[code]
            # A bare number beside the name of a different Instrument is far
            # more often a price (台積電 2500 撐住) than a second stock.
            if named and code.isdigit() and not code.startswith("0") and not (cands & named):
                continue
            found[code] = set(cands)
        return found


def resolve(alias: str, cands: set[str], text: str, comment_id: int,
            rules: dict[str, list], calls: dict | None = None) -> str | None:
    """The code this Alias means in this Comment: an Instrument, NOT, or None
    when it is ambiguous and nobody has decided yet. The user's rules win over
    everything, the most specific first; then an Alias Call, if it was made
    from the same candidates."""
    rs = rules.get(alias, [])
    for r in rs:
        if r["scope"] == "comment" and r["comment_id"] == comment_id:
            return r["code"]
    for r in rs:
        if r["scope"] == "context" and r["context"] and r["context"] in text:
            return r["code"]
    for r in rs:
        if r["scope"] == "always":
            return r["code"]
    if len(cands) == 1:
        return next(iter(cands))
    call = (calls or {}).get((comment_id, alias))
    if call and call[1] == candidates_key(cands):
        return call[0]
    return None


def candidates_key(cands: set[str]) -> str:
    return ",".join(sorted(cands))


def load_calls(con) -> dict[tuple[int, str], tuple[str, str]]:
    """{(comment_id, alias): (code, candidates)}: the model's Alias Calls."""
    return {(r["comment_id"], r["alias"]): (r["code"], r["candidates"])
            for r in con.execute("SELECT * FROM alias_calls")}


def load_rules(con) -> dict[str, list]:
    rules: dict[str, list] = {}
    for r in con.execute("SELECT * FROM alias_rules ORDER BY id"):
        rules.setdefault(r["alias"], []).append(dict(r))
    return rules
