"""Register evaluation commands and dispatch them."""

import argparse

from . import budget, dedupe, e2e, exam, loop, metrics, record, replay, report, serve, sheet


def main():
    parser = argparse.ArgumentParser(description="Brainstorm evaluation tools")
    subparsers = parser.add_subparsers(dest="command", required=True)
    budget.register(subparsers)
    exam.register(subparsers)
    record.register(subparsers)
    dedupe.register(subparsers)
    loop.register(subparsers)
    metrics.register(subparsers)
    replay.register(subparsers)
    sheet.register(subparsers)
    serve.register(subparsers)
    report.register(subparsers)
    e2e.register(subparsers)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
