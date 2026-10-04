# Forum Pulse

What PTT Stock talks about each day, how bullish or bearish it is, and whether
that attention was followed by out- or under-performance. Vocabulary is in
[CONTEXT.md](CONTEXT.md).

```
./run.sh daily              crawl → match Aliases → label Stances → prices → backtest → site
./run.sh daily --no-label   same, without calling Claude
./run.sh serve              dashboard + Review Queue at http://127.0.0.1:8765
./run.sh rebuild            re-apply Review Queue rules and republish (nothing fetched)
./run.sh test
```

The dashboard also opens straight from disk (`site/index.html`); only the
Review Queue needs `serve`.

## Setup

1. `cp .env.example .env` and set `ANTHROPIC_API_KEY`. Without it everything
   runs except Stance labelling.
2. Cron runs `daily` at 15:30 and 23:50 Taipei time (see `crontab -l`). Runs
   take a lock, so overlapping firings skip themselves.

## How it works

| Step | Module | Notes |
|---|---|---|
| Crawl | `ptt.py` | Board index pages back to 2025-07-01, then each Post. Posts under 3 days old are re-read for new pushes. |
| Match | `instruments.py` | TWSE/TPEx listings (stocks, ETFs, TDRs) + `slang.json`. Numbers that look like dates, times, prices or ranges are not codes. |
| Review | `server.py`, `web/review.*` | Ambiguous Aliases (長榮, 統一, 世界…) count for nothing until you decide: always, by context word, or one Comment. |
| Mentions | `measure.py` | One per user × Instrument × Forum Day. |
| Stance | `stance.py` | Claude Haiku 4.5 labels Mentions in Signals and of the Market, 25 per request, seeing the Post title and the 3 pushes before each line. 標的 Posts ending 多/空 give an Author Stance, which overrides the model and is used to check it. A run spends at most `LLM_BUDGET_USD_PER_RUN` ($5); the next run continues. |
| Prices | `prices.py` | yfinance dividend-adjusted open/close; benchmark is the TAIEX total-return index (TWSE MFI94U). |
| Backtest | `backtest.py` | Entry = next trading day's open; 1D/1W/1M/3M = 1/5/21/63 trading days; Excess Return vs TAIEX TR. The Market is judged on its raw return. |
| Site | `site.py`, `web/` | One JS data file per Forum Day and per Instrument. |

Tunables (Top N, spike ratio, Stance Group cut-off, budget) are in
`forum_pulse/config.py`.

## Known limits

- Dcard is not collected (it blocks scripted access). The `forum` column is
  there for when it is.
- A plain 4-digit number next to a *different* stock's name is treated as a
  price, so 「台積電 2500 撐住 2454 也不錯」 misses 2454.
- Overlapping Horizons mean Signals are far from independent; read n as an
  upper bound on the evidence.
