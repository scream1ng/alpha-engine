"""Alias for run.py — use: python run.py"""
from run import interactive, cli
import sys

if __name__ == "__main__":
    if len(sys.argv) == 1:
        interactive()
    else:
        cli()
