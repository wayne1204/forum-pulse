from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from forum_pulse import config, db, instruments, ptt

FIX = Path(__file__).parent / "fixtures"


# --- fetching PTT ------------------------------------------------------------------------

@pytest.fixture
def responses(monkeypatch):
    """Queue what successive GETs return: a status code or an exception."""
    queue = []

    def get(url, timeout):
        r = queue.pop(0)
        if isinstance(r, Exception):
            raise r

        def raise_for_status():
            if r >= 400:
                raise requests.HTTPError(str(r))
        return SimpleNamespace(status_code=r, text=f"page {url}", raise_for_status=raise_for_status)
    monkeypatch.setattr(ptt, "_session", lambda: SimpleNamespace(get=get))
    monkeypatch.setattr(ptt.time, "sleep", lambda s: None)
    return queue


def test_fetch_returns_the_page(responses):
    responses.append(200)
    assert ptt.fetch("u") == "page u"


def test_a_deleted_post_is_none(responses):
    responses.append(404)
    assert ptt.fetch("u") is None


def test_fetch_retries_then_gives_up(responses):
    responses.extend([requests.ConnectionError(), 503, 200])
    assert ptt.fetch("u") == "page u"
    responses.extend([503, 503])
    with pytest.raises(requests.HTTPError):
        ptt.fetch("u", retries=2)


def test_one_session_per_thread_past_the_age_check(monkeypatch):
    monkeypatch.setattr(ptt, "_local", ptt.threading.local())
    s = ptt._session()
    assert ptt._session() is s
    assert s.cookies.get("over18", domain="www.ptt.cc") == "1"


OLD_PAGE = """<div class="r-list-container">
<div class="r-ent"><div class="title"><a href="/bbs/Stock/M.1780000000.A.000.html">[閒聊] 舊</a></div>
<div class="author">old</div></div></div>
<a class="btn wide" href="/bbs/Stock/index10421.html">&lsaquo; 上頁</a>"""


def test_crawl_reads_new_posts_back_to_since(con, monkeypatch):
    target = (FIX / "post_target.html").read_text()
    pages = {
        "index.html": (FIX / "index.html").read_text(),         # two Posts from 2026-10-03
        "index10423.html": OLD_PAGE,                             # older than since: stop here
        "M.1791010505.A.157.html": target,
        "M.1791011815.A.363.html": "<html></html>",              # malformed
    }
    fetched = []

    def fetch(url):
        fetched.append(url.rsplit("/", 1)[1])
        return pages[fetched[-1]]
    monkeypatch.setattr(ptt, "fetch", fetch)
    monkeypatch.setattr(ptt.time, "sleep", lambda s: None)
    logs = []
    changed = ptt.crawl(con, date(2026, 10, 1), log=logs.append)
    assert changed == 1 + 58                                     # body + pushes
    assert "index10421.html" not in fetched
    assert [r[0] for r in con.execute("SELECT post_id FROM posts")] == ["M.1791010505.A.157"]
    assert any("skipped M.1791011815.A.363" in m for m in logs)


# --- the official listing ----------------------------------------------------------------

ISIN = """<table><tr><td colspan=7><B> 股票 <B></td></tr>
<tr><td bgcolor=#FAFAD2>2330　台積電</td><td>TW0002330008</td><td>1994/09/05</td><td>上市</td></tr>
<tr><td bgcolor=#FAFAD2>1101B　台泥乙特*</td><td>x</td><td>x</td><td>上市</td></tr>
<tr><td>無代號</td><td>x</td><td>x</td><td>上市</td></tr>
<tr><td>2330　short</td></tr>
<tr><td colspan=7><B> 上市認購(售)權證 <B></td></tr>
<tr><td>030001　權證</td><td>x</td><td>x</td><td>上市</td></tr>
<tr><td colspan=7><B> ETF <B></td></tr>
<tr><td>0050　元大台灣50</td><td>x</td><td>x</td><td>上市</td></tr>
</table>"""


def test_isin_listing_keeps_stocks_and_etfs():
    rows = instruments.parse_isin(ISIN, "TPEx")
    assert [(r["code"], r["name"], r["kind"], r["yf_symbol"]) for r in rows] == [
        ("2330", "台積電", "stock", "2330.TWO"), ("1101B", "台泥乙特", "stock", "1101B.TWO"),
        ("0050", "元大台灣50", "etf", "0050.TWO")]


def test_fetch_listed_reads_both_exchanges(monkeypatch):
    urls = []

    def get(url, headers, timeout):
        urls.append(url)
        return SimpleNamespace(text=ISIN, raise_for_status=lambda: None)
    monkeypatch.setattr(instruments.requests, "get", get)
    rows = instruments.fetch_listed()
    assert [u[-1] for u in urls] == ["2", "4"]
    assert {r["exchange"] for r in rows} == {"TWSE", "TPEx"} and len(rows) == 6


def test_the_shipped_slang_table_loads():
    slang = instruments.load_slang()
    assert slang["slang"] and isinstance(slang.get("maybe_nothing", []), list)


# --- the store -----------------------------------------------------------------------------

def test_session_commits_and_meta_round_trips():
    with db.session() as con:
        assert db.get_meta(con, "k", "none") == "none"
        db.set_meta(con, "k", 1)
        db.set_meta(con, "k", 2)
    assert config.DB_PATH.exists()
    with db.session() as con:
        assert db.get_meta(con, "k") == "2"
