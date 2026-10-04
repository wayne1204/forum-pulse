"""python -m forum_pulse daily | rebuild | serve [port]"""
import argparse

from . import pipeline, server


def main():
    ap = argparse.ArgumentParser(prog="forum_pulse")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("daily", help="crawl, match, label, price, backtest, publish")
    d.add_argument("--no-label", action="store_true", help="skip the model (no API spend)")
    d.add_argument("--budget", type=float, help="USD the model may spend this run")
    sub.add_parser("rebuild", help="apply Review Queue rules and republish, nothing fetched")
    s = sub.add_parser("serve", help="dashboard + Review Queue on 127.0.0.1")
    s.add_argument("port", nargs="?", type=int, default=8765)
    a = ap.parse_args()
    if a.cmd == "daily":
        pipeline.daily(label=not a.no_label, budget=a.budget)
    elif a.cmd == "rebuild":
        pipeline.rebuild()
    else:
        server.serve(a.port)


if __name__ == "__main__":
    main()
