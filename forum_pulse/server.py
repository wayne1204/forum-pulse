"""Serves the dashboard and the Review Queue's small API on 127.0.0.1."""
from __future__ import annotations

import json
import threading
from datetime import datetime
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from . import config, db, measure, pipeline
from .instruments import NOT

_state = {"running": False, "last": None}


def queue(con, per_alias: int = 15) -> list[dict]:
    names = {r["code"]: r["name"] for r in con.execute("SELECT code, name FROM instruments")}
    out = []
    for a in con.execute("SELECT alias, COUNT(*) n FROM hits WHERE code IS NULL GROUP BY alias ORDER BY n DESC"):
        cands = [r["code"] for r in con.execute("SELECT code FROM aliases WHERE alias=? ORDER BY code", (a["alias"],))]
        if NOT not in cands:
            cands.append(NOT)        # anything can turn out to be ordinary text
        comments = con.execute("""
            SELECT c.id, c.day, c.user, c.text, c.post_id, p.title FROM hits h
            JOIN comments c ON c.id=h.comment_id JOIN posts p USING(post_id)
            WHERE h.alias=? AND h.code IS NULL ORDER BY c.at DESC LIMIT ?""", (a["alias"], per_alias)).fetchall()
        out.append({"alias": a["alias"], "count": a["n"],
                    "candidates": [{"code": c, "name": names.get(c, "")} for c in cands],
                    "comments": [{"id": c["id"], "day": c["day"], "user": c["user"], "text": c["text"][:300],
                                  "title": c["title"],
                                  "url": f"{config.PTT_BASE}/bbs/{config.PTT_BOARD}/{c['post_id']}.html"}
                                 for c in comments]})
    return out


def add_rule(con, rule: dict) -> int:
    alias, scope, code = rule["alias"].strip(), rule["scope"], rule["code"].strip()
    if scope not in ("always", "context", "comment"):
        raise ValueError("scope must be always, context or comment")
    if code != NOT and not con.execute("SELECT 1 FROM instruments WHERE code=?", (code,)).fetchone():
        raise ValueError(f"unknown Instrument {code}")
    context = rule.get("context", "").strip() if scope == "context" else ""
    if scope == "context" and not context:
        raise ValueError("a context rule needs a context word")
    comment_id = int(rule.get("comment_id") or 0) if scope == "comment" else 0
    before = con.execute("SELECT COUNT(*) FROM hits WHERE code IS NULL").fetchone()[0]
    con.execute("INSERT OR REPLACE INTO alias_rules(alias, scope, context, comment_id, code) VALUES(?,?,?,?,?)",
                (alias, scope, context, comment_id, code))
    # A rule for an Alias nobody listed (a word to ignore, a new nickname)
    # still has to be found in text: make it a known Alias and search again.
    if not con.execute("SELECT 1 FROM aliases WHERE alias=?", (alias,)).fetchone() and code != NOT:
        con.execute("INSERT INTO aliases VALUES(?,?, 'user')", (alias, code))
        con.execute("UPDATE comments SET matched=0 WHERE text LIKE ?", (f"%{alias}%",))
        measure.match_comments(con, log=lambda *_: None)
    measure.reresolve(con)
    con.commit()
    after = con.execute("SELECT COUNT(*) FROM hits WHERE code IS NULL").fetchone()[0]
    return max(before - after, 0) if before != after else con.execute(
        "SELECT COUNT(*) FROM hits WHERE alias=?", (alias,)).fetchone()[0]


def _rebuild():
    try:
        pipeline.rebuild(log=lambda m: print(m, flush=True))
    finally:
        _state["running"] = False
        _state["last"] = datetime.now(config.TZ).strftime("%Y-%m-%d %H:%M")


class Handler(SimpleHTTPRequestHandler):
    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self):
        if not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def do_GET(self):
        if not self.path.startswith("/api/"):
            return super().do_GET()
        with db.session() as con:
            if self.path == "/api/queue":
                return self._json(queue(con))
            if self.path == "/api/rules":
                return self._json([dict(r) for r in con.execute(
                    "SELECT r.*, i.name FROM alias_rules r LEFT JOIN instruments i ON i.code=r.code ORDER BY r.id DESC")])
            if self.path == "/api/status":
                return self._json(_state)
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        try:
            if self.path == "/api/rule":
                with db.session() as con:
                    return self._json({"resolved": add_rule(con, body)})
            if self.path == "/api/rule/delete":
                with db.session() as con:
                    con.execute("DELETE FROM alias_rules WHERE id=?", (int(body["id"]),))
                    measure.reresolve(con)
                return self._json({"ok": True})
            if self.path == "/api/rebuild":
                if not _state["running"]:
                    _state["running"] = True
                    threading.Thread(target=_rebuild, daemon=True).start()
                return self._json(_state)
        except (ValueError, KeyError) as e:
            return self._json({"error": str(e)}, 400)
        self._json({"error": "not found"}, 404)

    def log_message(self, fmt, *args):
        if "/api/" in (args[0] if args else ""):
            super().log_message(fmt, *args)


def serve(port: int = 8765):
    config.SITE_DIR.mkdir(parents=True, exist_ok=True)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), partial(Handler, directory=str(config.SITE_DIR)))
    print(f"Forum Pulse on http://127.0.0.1:{port}/  (Review Queue: /review.html)", flush=True)
    httpd.serve_forever()
