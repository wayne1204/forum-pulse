"""PTT Stock: walk the board's index pages, read each Post and its pushes into
Comments. Parsing is kept apart from fetching so it can be tested on saved HTML."""
from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

import requests
from bs4 import BeautifulSoup

from . import config

FORUM = "ptt"
_POST_ID = re.compile(r"/bbs/\w+/(M\.(\d+)\.A\.[0-9A-F]{3})\.html")
_PREV = re.compile(r'href="/bbs/\w+/index(\d+)\.html">&lsaquo; 上頁')
_TYPE = re.compile(r"^(?:(?:Re|Fw)\s*:\s*)*\[([^\]]{1,6})\]")
_PUSH_TIME = re.compile(r"(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})")
_FOOTER = re.compile(r"\n--\n※ 發信站|\n※ 發信站")

_local = threading.local()


def _session() -> requests.Session:
    s = getattr(_local, "s", None)
    if s is None:
        s = requests.Session()
        s.headers.update(config.HTTP_HEADERS)
        s.cookies.set("over18", "1", domain="www.ptt.cc")
        _local.s = s
    return s


def fetch(url: str, retries: int = 4) -> str | None:
    """The page's HTML, or None for a Post that is gone (404)."""
    delay = 2.0
    for attempt in range(retries):
        try:
            r = _session().get(url, timeout=config.HTTP_TIMEOUT)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.text
        except requests.RequestException:
            if attempt == retries - 1:
                raise
            time.sleep(delay)
            delay *= 2
    return None


# --- parsing -----------------------------------------------------------------

def post_time_from_id(post_id: str) -> datetime:
    """A PTT Post id carries its creation time: M.<unix seconds>.A.xxx"""
    return datetime.fromtimestamp(int(post_id.split(".")[1]), config.TZ)


def post_type(title: str) -> str | None:
    m = _TYPE.match(title.strip())
    return m.group(1).strip() if m else None


def parse_index(html: str) -> tuple[list[dict], int | None]:
    """Posts listed on one index page (pinned ones left out) and the number of
    the page before it."""
    soup = BeautifulSoup(html, "lxml")
    posts = []
    container = soup.select_one("div.r-list-container")
    for el in container.children if container else []:
        if getattr(el, "get", None) is None:
            continue
        classes = el.get("class") or []
        if "r-list-sep" in classes:
            break                      # everything below is pinned
        if "r-ent" not in classes:
            continue
        a = el.select_one("div.title a")
        if a is None:
            continue                   # deleted
        m = _POST_ID.search(a["href"])
        if not m:
            continue
        posts.append({"post_id": m.group(1), "title": a.get_text(strip=True),
                      "author": el.select_one("div.author").get_text(strip=True)})
    m = _PREV.search(html)
    return posts, (int(m.group(1)) if m else None)


def _push_time(raw: str, posted: datetime) -> datetime:
    m = _PUSH_TIME.search(raw or "")
    if not m:
        return posted
    mo, d, h, mi = map(int, m.groups())
    # Pushes carry no year. A Post keeps taking pushes for months, so a push
    # dated well before its Post (a July Post, a March push) is from the next
    # year; a day or two before is just a header showing the last edit time.
    year = posted.year
    if (mo, d) < (posted.month, posted.day) and (posted - timedelta(days=2)).replace(tzinfo=None) > datetime(year, mo, d, h, mi):
        year += 1
    try:
        t = datetime(year, mo, d, h, mi, tzinfo=config.TZ)
    except ValueError:
        return posted
    # A Post moved or re-created on the board keeps pushes older than its id;
    # never date one in the future.
    if t > datetime.now(config.TZ) + timedelta(hours=1):
        t = t.replace(year=t.year - 1)
    return t


def parse_post(html: str, post_id: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    main = soup.select_one("#main-content")
    meta = {}
    for line in main.select("div.article-metaline, div.article-metaline-right"):
        tag, val = line.select_one(".article-meta-tag"), line.select_one(".article-meta-value")
        if tag and val:
            meta[tag.get_text(strip=True)] = val.get_text(strip=True)
    # Pushes are dated against when the Post was created (its id), never its
    # header time, which moves forward when the author edits.
    created = posted = post_time_from_id(post_id)
    if meta.get("時間"):
        try:
            posted = datetime.strptime(" ".join(meta["時間"].split()),
                                       "%a %b %d %H:%M:%S %Y").replace(tzinfo=config.TZ)
        except ValueError:
            pass
    author = (meta.get("作者") or "").split(" ")[0]

    pushes = []
    for p in main.select("div.push"):
        tag = p.select_one(".push-tag")
        user = p.select_one(".push-userid")
        content = p.select_one(".push-content")
        if not (tag and user and content):
            continue               # "檔案過大" warnings and the like
        text = content.get_text().lstrip(":").strip()
        ipdt = p.select_one(".push-ipdatetime")
        pushes.append({"tag": tag.get_text(strip=True), "user": user.get_text(strip=True),
                       "text": text, "at": _push_time(ipdt.get_text() if ipdt else "", created)})
        p.decompose()
    for el in main.select("div.article-metaline, div.article-metaline-right, span.f2"):
        el.decompose()
    body = _FOOTER.split(main.get_text(), maxsplit=1)[0].strip()

    title = meta.get("標題") or ""
    return {"post_id": post_id, "title": title, "author": author, "posted_at": posted,
            "body": body, "pushes": pushes}


# --- storing -----------------------------------------------------------------

def store_post(con, post: dict, forum: str = FORUM) -> int:
    """Write a Post and its Comments. Comments are keyed by position, so a
    re-read keeps the ids of everything that did not change. Returns how many
    Comments are new or changed."""
    now = datetime.now(config.TZ).isoformat(timespec="seconds")
    con.execute(
        "INSERT INTO posts(post_id, forum, title, post_type, author, posted_at, fetched_at) "
        "VALUES(?,?,?,?,?,?,?) ON CONFLICT(post_id) DO UPDATE SET "
        "title=excluded.title, post_type=excluded.post_type, fetched_at=excluded.fetched_at",
        (post["post_id"], forum, post["title"], post_type(post["title"]), post["author"],
         post["posted_at"].isoformat(), now))
    rows = [("body", post["author"], post["body"], post["posted_at"])]
    rows += [(p["tag"], p["user"], p["text"], p["at"]) for p in post["pushes"]]
    existing = {r["seq"]: r for r in con.execute(
        "SELECT id, seq, user, text, at FROM comments WHERE post_id=?", (post["post_id"],))}
    changed = 0
    for seq, (tag, user, text, at) in enumerate(rows):
        old = existing.get(seq)
        iso = at.isoformat()
        if old and (old["user"], old["text"], old["at"]) == (user, text, iso):
            continue
        changed += 1
        if old:
            con.execute("DELETE FROM hits WHERE comment_id=?", (old["id"],))
            con.execute("UPDATE comments SET user=?, tag=?, text=?, at=?, day=?, matched=0 "
                        "WHERE id=?", (user, tag, text, iso, at.date().isoformat(), old["id"]))
        else:
            con.execute("INSERT INTO comments(post_id, seq, forum, user, tag, text, at, day) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (post["post_id"], seq, forum, user, tag, text, iso,
                         at.date().isoformat()))
    gone = [r["id"] for s, r in existing.items() if s >= len(rows)]
    for cid in gone:
        con.execute("DELETE FROM hits WHERE comment_id=?", (cid,))
        con.execute("DELETE FROM comments WHERE id=?", (cid,))
    return changed


# --- crawling ----------------------------------------------------------------

def _index_url(n: int | None) -> str:
    page = f"index{n}.html" if n else "index.html"
    return f"{config.PTT_BASE}/bbs/{config.PTT_BOARD}/{page}"


def crawl(con, since: date, log=print) -> int:
    """Read every Post written on or after `since` that is not stored yet, and
    re-read the ones young enough to still be gaining pushes."""
    now = datetime.now(config.TZ)
    recrawl_after = now - timedelta(days=config.RECRAWL_DAYS)
    since_dt = datetime(since.year, since.month, since.day, tzinfo=config.TZ)
    known = {r[0] for r in con.execute("SELECT post_id FROM posts WHERE forum=?", (FORUM,))}

    todo: list[str] = []
    page: int | None = None
    pages = 0
    while True:
        html = fetch(_index_url(page))
        posts, prev = parse_index(html)
        pages += 1
        oldest = None
        for p in posts:
            t = post_time_from_id(p["post_id"])
            oldest = t if oldest is None else min(oldest, t)
            if t < since_dt:
                continue
            if p["post_id"] not in known or t >= recrawl_after:
                todo.append(p["post_id"])
        if pages % 100 == 0:
            log(f"  index pages read: {pages}, back to {oldest:%Y-%m-%d}, posts queued: {len(todo)}")
        if prev is None or (oldest is not None and oldest < since_dt):
            break
        page = prev
        time.sleep(0.2)
    log(f"  {pages} index pages, {len(todo)} posts to read")

    todo = list(dict.fromkeys(todo))
    url = lambda pid: f"{config.PTT_BASE}/bbs/{config.PTT_BOARD}/{pid}.html"
    changed = 0
    done = 0
    with ThreadPoolExecutor(config.CRAWL_WORKERS) as pool:
        # Fetch in parallel, write on this thread: sqlite connections are not shared.
        for i in range(0, len(todo), 60):
            chunk = todo[i:i + 60]
            for pid, html in zip(chunk, pool.map(lambda pid: fetch(url(pid)), chunk)):
                if html is None:
                    continue
                try:
                    changed += store_post(con, parse_post(html, pid))
                except Exception as e:          # one malformed Post must not stop a backfill
                    log(f"  skipped {pid}: {e!r}")
            con.commit()
            done += len(chunk)
            if done % 600 == 0 or done == len(todo):
                log(f"  posts read: {done}/{len(todo)}")
    return changed
