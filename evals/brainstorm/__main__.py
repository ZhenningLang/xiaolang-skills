"""Register evaluation commands and dispatch them."""

import argparse

from . import budget


def main():
    parser = argparse.ArgumentParser(description="Brainstorm evaluation tools")
    subparsers = parser.add_subparsers(dest="command", required=True)
    budget.register(subparsers)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
