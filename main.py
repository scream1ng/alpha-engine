"""CLI entry point. Usage: python main.py [market] [command]"""
import argparse
import sys


def _init_db() -> None:
    from db.models import init_db
    init_db()


def run_market(market: str, command: str, args: argparse.Namespace) -> None:
    if market == "th":
        from scripts.run_th import main as run
    elif market == "us":
        from scripts.run_us import main as run
    elif market == "au":
        from scripts.run_au import main as run
    elif market == "crypto":
        from scripts.run_crypto import main as run
    elif market == "commodity":
        from scripts.run_commodity import main as run
    else:
        print(f"Unknown market: {market}. Choose: th us au crypto commodity")
        sys.exit(1)
    run(command=command, args=args)


def main() -> None:
    parser = argparse.ArgumentParser(description="AlphaEngine — multi-market signal system")
    parser.add_argument("market", choices=["th", "us", "au", "crypto", "commodity", "all"])
    parser.add_argument(
        "command",
        choices=["scan", "optimise", "paper", "validate", "live"],
        help=(
            "scan: generate today's signals | "
            "optimise: run walk-forward | "
            "paper: run paper trading session | "
            "validate: run full 3-phase validation | "
            "live: run live trading"
        ),
    )
    parser.add_argument("--capital", type=float, default=1_000_000, help="Starting capital")
    parser.add_argument("--symbols", type=int, default=None, help="Max symbols to process (default: all)")
    parser.add_argument("--strategies", nargs="+", help="Limit to specific strategies")
    parser.add_argument("--dry-run", action="store_true", help="No real orders")
    args = parser.parse_args()
    _init_db()

    if args.market == "all":
        for m in ["th", "us", "au", "crypto", "commodity"]:
            run_market(m, args.command, args)
    else:
        run_market(args.market, args.command, args)


if __name__ == "__main__":
    main()
