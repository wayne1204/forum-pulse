"""The SQLite store. Raw collection (posts, comments) is kept forever; everything
from hits onward is derived and can be rebuilt."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    post_id    TEXT PRIMARY KEY,          -- M.1790920910.A.717
    forum      TEXT NOT NULL,
    title      TEXT NOT NULL,
    post_type  TEXT,                      -- 標的, 閒聊, ...
    author     TEXT NOT NULL,
    posted_at  TEXT NOT NULL,             -- ISO, Taipei
    fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS comments (
    id       INTEGER PRIMARY KEY,
    post_id  TEXT NOT NULL REFERENCES posts(post_id),
    seq      INTEGER NOT NULL,            -- 0 = the body, then each push in order
    forum    TEXT NOT NULL,
    user     TEXT NOT NULL,
    tag      TEXT NOT NULL,               -- body | 推 | 噓 | →
    text     TEXT NOT NULL,
    at       TEXT NOT NULL,               -- ISO, Taipei
    day      TEXT NOT NULL,               -- Forum Day
    matched  INTEGER NOT NULL DEFAULT 0,  -- Aliases already looked for
    UNIQUE (post_id, seq)
);
CREATE INDEX IF NOT EXISTS comments_day ON comments(day);

CREATE TABLE IF NOT EXISTS instruments (
    code      TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    kind      TEXT NOT NULL,              -- stock | etf | market
    exchange  TEXT NOT NULL,              -- TWSE | TPEx | -
    yf_symbol TEXT
);
CREATE TABLE IF NOT EXISTS aliases (
    alias  TEXT NOT NULL,
    code   TEXT NOT NULL,                 -- an Instrument, or NOT for "maybe nothing"
    source TEXT NOT NULL,                 -- official | slang
    PRIMARY KEY (alias, code)
);
-- The user's decisions about Ambiguous Aliases.
--   scope 'always'  : this Alias always means `code`
--   scope 'context' : ... when the Comment contains `context`
--   scope 'comment' : ... in Comment `comment_id` only
-- code NOT means Not an Instrument.
CREATE TABLE IF NOT EXISTS alias_rules (
    id         INTEGER PRIMARY KEY,
    alias      TEXT NOT NULL,
    scope      TEXT NOT NULL,
    context    TEXT NOT NULL DEFAULT '',
    comment_id INTEGER NOT NULL DEFAULT 0,
    code       TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (alias, scope, context, comment_id)
);
-- Every Alias found in a Comment. code is NULL while it waits in the Review Queue.
CREATE TABLE IF NOT EXISTS hits (
    comment_id INTEGER NOT NULL REFERENCES comments(id),
    alias      TEXT NOT NULL,
    code       TEXT,
    PRIMARY KEY (comment_id, alias)
);
-- The model's verdict on an Ambiguous Alias in one Comment, used where the
-- user has no rule. Kept apart from hits, which are rebuilt; `candidates` is
-- what it chose from, so a call made before the Aliases changed is asked again.
CREATE TABLE IF NOT EXISTS alias_calls (
    comment_id INTEGER NOT NULL,
    alias      TEXT NOT NULL,
    code       TEXT NOT NULL,             -- an Instrument, or NOT
    candidates TEXT NOT NULL,             -- sorted, comma-separated
    PRIMARY KEY (comment_id, alias)
);
CREATE TABLE IF NOT EXISTS mentions (
    forum      TEXT NOT NULL,
    day        TEXT NOT NULL,
    user       TEXT NOT NULL,
    code       TEXT NOT NULL,
    n_comments INTEGER NOT NULL,
    PRIMARY KEY (forum, day, user, code)
);
CREATE INDEX IF NOT EXISTS mentions_day ON mentions(day, code);
-- Kept apart from mentions so a rebuild never throws away paid-for labels.
-- n_comments records what was read; a Mention that has grown is labelled again.
CREATE TABLE IF NOT EXISTS stances (
    forum      TEXT NOT NULL,
    day        TEXT NOT NULL,
    user       TEXT NOT NULL,
    code       TEXT NOT NULL,
    stance     TEXT NOT NULL,             -- bullish | bearish | neutral | mixed
    source     TEXT NOT NULL,             -- author | model
    n_comments INTEGER NOT NULL,
    model_stance TEXT,                    -- the model's view of an Author Stance
    PRIMARY KEY (forum, day, user, code)
);
CREATE TABLE IF NOT EXISTS prices (
    symbol TEXT NOT NULL,                 -- Instrument code, or BENCHMARK
    day    TEXT NOT NULL,
    open   REAL,
    close  REAL,
    PRIMARY KEY (symbol, day)
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect(path=None) -> sqlite3.Connection:
    path = path or config.DB_PATH
    if str(path) != ":memory:":
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=60)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA)
    return con


@contextmanager
def session(path=None):
    con = connect(path)
    try:
        yield con
        con.commit()
    finally:
        con.close()


def get_meta(con, key, default=None):
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(con, key, value):
    con.execute("INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
