"""Every tunable in one place. Terms follow CONTEXT.md."""
from __future__ import annotations

from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "forum_pulse.db"
SITE_DIR = ROOT / "site"
SLANG_PATH = ROOT / "forum_pulse" / "slang.json"

TZ = ZoneInfo("Asia/Taipei")

# --- collection --------------------------------------------------------------
BACKFILL_START = date(2025, 7, 1)
PTT_BOARD = "Stock"
PTT_BASE = "https://www.ptt.cc"
# Comments keep arriving on Posts for days; Posts this young are re-read each run.
RECRAWL_DAYS = 3
CRAWL_WORKERS = 3
HTTP_TIMEOUT = 30
HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (forum-pulse; personal research)"}

# --- measuring ----------------------------------------------------------------
TOP_N = 10
SPIKE_RATIO = 3.0
SPIKE_LOOKBACK = 20          # trading days
SPIKE_MIN_MENTIONS = 5

STANCE_GROUP_CUTOFF = 0.3
STANCE_GROUP_MIN_DECIDED = 5  # bullish + bearish Mentions a Signal needs

# --- the model ----------------------------------------------------------------
LLM_MODEL = "claude-haiku-4-5"
LLM_PRICE_IN = 1.00 / 1e6    # USD per token
LLM_PRICE_OUT = 5.00 / 1e6
LLM_ITEMS_PER_REQUEST = 25
LLM_WORKERS = 4
LLM_CONTEXT_PUSHES = 3       # pushes shown before each of the user's own
# A backfill could label hundreds of thousands of Mentions; one run spends at
# most this much and the next run carries on where it stopped.
LLM_BUDGET_USD_PER_RUN = 5.00

# --- testing what followed ------------------------------------------------------
HORIZONS = {"1D": 1, "1W": 5, "1M": 21, "3M": 63}
MARKET = "MARKET"            # the Market Instrument's code
BENCHMARK = "TAIEX_TR"       # its price series: TAIEX total-return index
