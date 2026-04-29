"""AlphaEngine interactive menu. Run: python run.py"""
from __future__ import annotations
import argparse
import sys
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

MARKETS = [
    ("th",        "Thailand SET"),
    ("us",        "US Equities"),
    ("au",        "AU ASX"),
    ("crypto",    "Crypto"),
    ("commodity", "Commodity / Futures"),
    ("all",       "All markets"),
]

INTERACTIVE_COMMANDS = [
    ("quick-report", "3yr annual return by strategy & filter phase"),
    ("optimise-filter", "Phase 1 — optimise trend / RVol / RSM filters"),
    ("optimise-risk", "Phase 2 — optimise SL / TP / BE / hard stop"),
    ("report",   "View last optimise result (instant)"),
    ("scan",     "Generate today's signals (live strategies only)"),
    ("paper",    "Paper trading simulation (last 90 days)"),
]

CLI_COMMANDS = [
    ("quick-report", "3yr annual return by strategy & filter phase"),
    ("optimise", "Walk-forward optimise — find best params"),
    ("optimise-filter", "Phase 1 — optimise trend / RVol / RSM filters"),
    ("optimise-risk", "Phase 2 — optimise SL / TP / BE / hard stop"),
    ("report",   "View last optimise result (instant)"),
    ("scan",     "Generate today's signals (live strategies only)"),
    ("paper",    "Paper trading simulation (last 90 days)"),
    ("diagnose", "Check how many signals each strategy fires"),
    ("validate", "optimise + paper in sequence"),
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
    """Print numbered menu, return selected key. Accepts number or key."""
    W = 52
    print()
    print("  ┌" + "─" * W + "┐")
    print(f"  │  {title:<{W-2}}│")
    print("  ├" + "─" * W + "┤")
    for i, (key, label) in enumerate(options, 1):
        row = f"  {i}.  {key:<12}  {label}"
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


def _run_market(market: str, command: str, args: argparse.Namespace) -> None:
    from scripts.pipeline import run
    adapter = _get_adapter(market)
    run(adapter, command, args)


def interactive() -> None:
    print()
    print("  ╔══════════════════════════════════════════════════════╗")
    print("  ║            ALPHA ENGINE  — signal system            ║")
    print("  ╚══════════════════════════════════════════════════════╝")

    market = _menu("SELECT MARKET", MARKETS)
    command = _menu("SELECT COMMAND", INTERACTIVE_COMMANDS)

    # Capital stays as an internal sizing baseline; interactive flows default it.
    capital = 1_000_000
    symbols = None
    if command in ("optimise", "optimise-filter", "optimise-risk", "validate", "diagnose", "paper"):
        symbols = _parse_symbols(_ask("Symbols (blank/all = all above turnover)", "all"))

    args = argparse.Namespace(capital=capital, symbols=symbols, dry_run=False, strategy_jobs=1)

    from db.models import init_db
    init_db()

    targets = ALL_MARKETS if market == "all" else [market]
    for m in targets:
        if len(targets) > 1:
            print(f"\n{'='*60}\n  MARKET: {m.upper()}\n{'='*60}")
        _run_market(m, command, args)


def cli() -> None:
    """Non-interactive mode: python run.py th report"""
    parser = argparse.ArgumentParser(prog="python run.py")
    parser.add_argument("market", choices=[m for m, _ in MARKETS])
    parser.add_argument("command", choices=[c for c, _ in CLI_COMMANDS])
    parser.add_argument("--capital", type=float, default=1_000_000)
    parser.add_argument("--symbols", type=int)
    parser.add_argument("--strategy-jobs", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

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
