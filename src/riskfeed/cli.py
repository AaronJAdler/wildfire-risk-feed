"""Command line for the feed.

riskfeed tick    [--state state/feed.duckdb] [--out public/]    one live run
riskfeed replay  fixtures/caldor-2021 [--state ...] [--out ...]   every recorded tick in order
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import httpx

from riskfeed import fixtures
from riskfeed.config import Settings
from riskfeed.delivery import FileSink, Sink, WebhookSink
from riskfeed.pipeline import run_tick
from riskfeed.store import Store


def _sinks(settings: Settings, out: Path, client: httpx.Client) -> list[Sink]:
    sinks: list[Sink] = [FileSink(out / "notifications.jsonl")]
    if settings.webhook_url:
        sinks.append(WebhookSink(settings.webhook_url, client))
    return sinks


def _tick(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    store = Store(args.state)
    out = Path(args.out)
    with httpx.Client(follow_redirects=True) as client:
        summary = run_tick(settings, store, client, out, _sinks(settings, out, client))
    store.close()
    print(json.dumps(summary.as_dict(), indent=2))
    return 0


def _replay(args: argparse.Namespace) -> int:
    settings = Settings()
    store = Store(args.state)
    out = Path(args.out)
    for tick in fixtures.ticks(args.scenario):
        with httpx.Client(transport=fixtures.transport(args.scenario, tick.name)) as client:
            summary = run_tick(
                settings,
                store,
                client,
                out,
                [FileSink(out / "notifications.jsonl")],
                now=tick.now,
                sleep=lambda _: None,
            )
        health = ", ".join(f"{k} {v['health']}" for k, v in summary.sources.items())
        print(f"== {tick.name} at {tick.now:%Y-%m-%d %H:%M} UTC: {summary.completeness} ({health})")
        print(f"   cells by level: {summary.levels}")
        for t in summary.transitions:
            print(f"   {t.name:<10} {t.title}")
    store.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="riskfeed", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
    tick = commands.add_parser("tick", help="fetch live data and publish")
    replay = commands.add_parser("replay", help="run a recorded scenario")
    replay.add_argument("scenario")
    for sub, state in ((tick, "state/feed.duckdb"), (replay, ".replay/feed.duckdb")):
        sub.add_argument("--state", default=state)
        sub.add_argument("--out", default="public" if sub is tick else ".replay/public")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    return _tick(args) if args.command == "tick" else _replay(args)


if __name__ == "__main__":
    sys.exit(main())
