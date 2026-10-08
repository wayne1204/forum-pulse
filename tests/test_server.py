import json
import threading
import time
import urllib.error
import urllib.request
from functools import partial
from http.server import ThreadingHTTPServer

import pytest

from conftest import DAY, add_post
from forum_pulse import config, measure, server
from forum_pulse.instruments import NOT


@pytest.fixture
def queued(world):
    add_post(world, "M.1790920910.A.717", "[閒聊] 盤中", "host", DAY, [("u1", "長榮噴"), ("u2", "積積噴")])
    measure.match_comments(world, log=lambda *_: None)
    world.commit()
    return world


def test_queue_lists_ambiguous_aliases_with_their_comments(queued):
    [item] = server.queue(queued)
    assert item["alias"] == "長榮" and item["count"] == 1
    assert [c["code"] for c in item["candidates"]] == ["2603", "2618", NOT]
    [c] = item["comments"]
    assert c["user"] == "u1" and c["url"].endswith("/bbs/Stock/M.1790920910.A.717.html")


def test_a_rule_resolves_the_queue(queued):
    assert server.add_rule(queued, {"alias": "長榮", "scope": "always", "code": "2603"}) == 1
    assert server.queue(queued) == []
    assert queued.execute("SELECT code FROM hits WHERE alias='長榮'").fetchone()[0] == "2603"


def test_a_rule_for_a_new_nickname_finds_it_in_old_comments(queued):
    assert server.add_rule(queued, {"alias": "積積", "scope": "always", "code": "2330"}) == 1
    assert queued.execute("SELECT code FROM hits WHERE alias='積積'").fetchone()[0] == "2330"


@pytest.mark.parametrize("rule, msg", [
    ({"alias": "長榮", "scope": "forever", "code": "2603"}, "scope"),
    ({"alias": "長榮", "scope": "always", "code": "9999"}, "unknown Instrument"),
    ({"alias": "長榮", "scope": "context", "code": "2603", "context": " "}, "context word"),
])
def test_bad_rules_are_refused(queued, rule, msg):
    with pytest.raises(ValueError, match=msg):
        server.add_rule(queued, rule)


# --- over HTTP ------------------------------------------------------------------------

@pytest.fixture
def http(queued, monkeypatch):
    config.SITE_DIR.mkdir(parents=True)
    (config.SITE_DIR / "index.html").write_text("<h1>dashboard</h1>")
    monkeypatch.setitem(server._state, "running", False)
    monkeypatch.setitem(server._state, "last", None)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(server.Handler, directory=str(config.SITE_DIR)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def call(path, body=None):
        data = None if body is None else json.dumps(body).encode()
        try:
            with urllib.request.urlopen(base + path, data=data) as r:
                raw, status = r.read(), r.status
        except urllib.error.HTTPError as e:
            raw, status = e.read(), e.code
        try:
            return status, json.loads(raw)
        except ValueError:
            return status, raw.decode()
    yield call
    httpd.shutdown()
    httpd.server_close()


def test_static_pages_are_served(http):
    assert http("/index.html") == (200, "<h1>dashboard</h1>")


def test_a_missing_file_is_a_404(http):
    assert http("/favicon.ico")[0] == 404


def test_review_queue_api(http):
    status, q = http("/api/queue")
    assert status == 200 and q[0]["alias"] == "長榮"
    assert http("/api/rule", {"alias": "長榮", "scope": "comment", "code": NOT,
                             "comment_id": q[0]["comments"][0]["id"]}) == (200, {"resolved": 1})
    assert http("/api/queue") == (200, [])
    status, rules = http("/api/rules")
    assert [(r["alias"], r["scope"], r["code"]) for r in rules] == [("長榮", "comment", NOT)]
    assert http("/api/rule/delete", {"id": rules[0]["id"]}) == (200, {"ok": True})
    assert http("/api/rules") == (200, [])
    assert http("/api/queue")[1][0]["alias"] == "長榮"


def test_bad_requests(http):
    assert http("/api/rule", {"alias": "長榮", "scope": "nope", "code": "2603"})[0] == 400
    assert http("/api/rule", {"scope": "always"})[0] == 400
    assert http("/api/nope")[0] == 404
    assert http("/api/nope", {})[0] == 404


def test_rebuild_runs_in_the_background(http, monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(server.pipeline, "rebuild", lambda log: release.wait(5))
    status, state = http("/api/rebuild", {})
    assert status == 200 and state["running"] is True
    assert http("/api/rebuild", {})[1]["running"] is True     # a second click joins the first
    release.set()
    for _ in range(50):
        if not http("/api/status")[1]["running"]:
            break
        time.sleep(0.05)
    status, state = http("/api/status")
    assert state["running"] is False and state["last"]


def test_serve_binds_to_localhost(monkeypatch):
    seen = {}

    class FakeServer:
        def __init__(self, addr, handler):
            seen["addr"] = addr

        def serve_forever(self):
            seen["served"] = True
    monkeypatch.setattr(server, "ThreadingHTTPServer", FakeServer)
    server.serve(9999)
    assert seen == {"addr": ("127.0.0.1", 9999), "served": True}
    assert config.SITE_DIR.is_dir()
