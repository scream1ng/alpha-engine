"""AlphaEngine interactive menu. Run: python run.py"""
from __future__ import annotations
import argparse
import io
import sys
import logging

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "buffer"):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

MARKETS = [
    ("th",        "Thailand SET"),
    ("us",        "US Equities"),
    ("au",        "AU ASX"),
    ("crypto",    "Crypto"),
    ("commodity", "Commodity / Futures"),
    ("all",       "All markets"),
]

COMMANDS = [
    ("run-all",         "Full pipeline: regime → optimise → stability → report → chart-export"),
    ("report",          "Build and view the latest research summary"),
    ("regime",          "5yr regime discovery — which strategies suit uptrend/choppy/downtrend"),
    ("optimise",        "TP exit optimisation on PASS regime pairs"),
    ("stability",       "Review regime-window stability of baseline and optimised pairs"),
    ("chart-export",    "Export optimised backtests → docs/chart_data.json for web viewer"),
    ("serve",           "Start local web server → http://localhost:8000  (Ctrl+C to stop)"),
]

_ADAPTERS = {
    "th":        ("markets.th",        "THAdapter"),
    "us":        ("markets.us",        "USAdapter"),
    "au":        ("markets.au",        "AUAdapter"),
    "crypto":    ("markets.crypto",    "CryptoAdapter"),
    "commodity": ("markets.commodity", "CommodityAdapter"),
}

ALL_MARKETS = [m for m, _ in MARKETS if m != "all"]


def _get_adapter(market: str):
    import importlib
    mod_path, cls_name = _ADAPTERS[market]
    mod = importlib.import_module(mod_path)
    return getattr(mod, cls_name)()


def _menu(title: str, options: list[tuple[str, str]]) -> str:
    W = 52
    print()
    print("  ┌" + "─" * W + "┐")
    print(f"  │  {title:<{W-2}}│")
    print("  ├" + "─" * W + "┤")
    for i, (key, label) in enumerate(options, 1):
        row = f"  {i}.  {key:<14}  {label}"
        print(f"  │  {row:<{W-2}}│")
    print("  └" + "─" * W + "┘")

    keys = [k for k, _ in options]
    while True:
        raw = input("  › ").strip().lower()
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(options):
                return keys[idx]
        elif raw in keys:
            return raw
        print(f"  Enter 1–{len(options)} or a name from the list.")


def _ask(prompt: str, default: str) -> str:
    val = input(f"  {prompt} [{default}]: ").strip()
    return val if val else default


def _parse_symbols(raw: str) -> int | None:
    value = raw.strip().lower()
    if value in ("", "all", "0"):
        return None
    return int(value)


def _serve() -> None:
    import os
    import threading
    import webbrowser
    from http.server import HTTPServer, SimpleHTTPRequestHandler
    docs_dir = os.path.join(os.path.dirname(__file__), "docs")
    os.chdir(docs_dir)
    port = 8000
    httpd = HTTPServer(("", port), SimpleHTTPRequestHandler)
    print(f"\n  Serving http://localhost:{port}  (Ctrl+C to stop)\n")
    threading.Timer(0.5, lambda: webbrowser.open(f"http://localhost:{port}")).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  Server stopped.")


def _run_market(market: str, command: str, args: argparse.Namespace) -> None:
    from scripts.pipeline import run
    adapter = _get_adapter(market)
    run(adapter, command, args)


def interactive() -> None:
    print()
    print("  ╔══════════════════════════════════════════════════════╗")
    print("  ║            ALPHA ENGINE  — signal system            ║")
    print("  ╚══════════════════════════════════════════════════════╝")

    command = _menu("SELECT COMMAND", COMMANDS)
    if command == "serve":
        _serve()
        return
    market  = _menu("SELECT MARKET", MARKETS)

    symbols = None
    if command in ("run-all", "regime", "optimise"):
        symbols = _parse_symbols(_ask("Symbols (blank/all = all above turnover)", "all"))

    args = argparse.Namespace(capital=1_000_000, symbols=symbols, dry_run=False, strategy_filter=None)

    from db.models import init_db
    init_db()

    targets = ALL_MARKETS if market == "all" else [market]
    for m in targets:
        if len(targets) > 1:
            print(f"\n{'='*60}\n  MARKET: {m.upper()}\n{'='*60}")
        _run_market(m, command, args)


def cli() -> None:
    """Non-interactive mode: python run.py th regime"""
    parser = argparse.ArgumentParser(prog="python run.py")
    parser.add_argument("market", choices=[m for m, _ in MARKETS])
    parser.add_argument("command", choices=[c for c, _ in COMMANDS])
    parser.add_argument("--capital", type=float, default=1_000_000)
    parser.add_argument("--symbols", type=int)
    parser.add_argument("--strategy", dest="strategy_filter", default=None,
                        help="Limit optimise to one strategy (e.g. pivot_breakout)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.command == "serve":
        _serve()
        return

    from db.models import init_db
    init_db()

    targets = ALL_MARKETS if args.market == "all" else [args.market]
    for m in targets:
        if len(targets) > 1:
            print(f"\n{'='*60}\n  MARKET: {m.upper()}\n{'='*60}")
        _run_market(m, args.command, args)


if __name__ == "__main__":
    if len(sys.argv) == 1:
        interactive()
    else:
        cli()
