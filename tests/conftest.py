from datetime import datetime

import pytest

from forum_pulse import config, db, instruments, ptt

TZ = config.TZ
DAY = datetime(2026, 10, 2, 10, tzinfo=TZ)

LISTED = [
    {"code": "2330", "name": "台積電", "kind": "stock", "exchange": "TWSE", "yf_symbol": "2330.TW"},
    {"code": "2603", "name": "長榮", "kind": "stock", "exchange": "TWSE", "yf_symbol": "2603.TW"},
    {"code": "2618", "name": "長榮航", "kind": "stock", "exchange": "TWSE", "yf_symbol": "2618.TW"},
    {"code": "2489", "name": "瑞軒", "kind": "stock", "exchange": "TWSE", "yf_symbol": "2489.TW"},
    {"code": "1216", "name": "統一", "kind": "stock", "exchange": "TWSE", "yf_symbol": "1216.TW"},
    {"code": "00631L", "name": "元大台灣50正2", "kind": "etf", "exchange": "TWSE", "yf_symbol": "00631L.TW"},
]
SLANG = {"slang": {"GG": "2330", "長榮": ["2603", "2618"], "大盤": "MARKET", "正2": "00631L"},
         "maybe_nothing": ["統一"]}


@pytest.fixture(autouse=True)
def _sandbox(tmp_path, monkeypatch):
    """No test may touch the real data/ or site/ directories."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "forum_pulse.db")
    monkeypatch.setattr(config, "SITE_DIR", tmp_path / "site")


@pytest.fixture
def con():
    c = db.connect(":memory:")
    instruments.refresh(c, LISTED, SLANG)
    return c


@pytest.fixture
def world():
    """A file database at config.DB_PATH, for code that opens its own
    connections (stance workers, the server, the pipeline)."""
    c = db.connect()
    instruments.refresh(c, LISTED, SLANG)
    c.commit()
    yield c
    c.close()


def add_post(con, pid, title, author, when, pushes, body="內文"):
    ptt.store_post(con, {"post_id": pid, "title": title, "author": author, "posted_at": when,
                         "body": body, "pushes": [{"tag": "推", "user": u, "text": t, "at": when} for u, t in pushes]})
