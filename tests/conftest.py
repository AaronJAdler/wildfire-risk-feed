from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest

from riskfeed import fixtures
from riskfeed.config import Settings
from riskfeed.delivery import FileSink
from riskfeed.pipeline import RunSummary, run_tick
from riskfeed.store import Store

SCENARIO = Path(__file__).resolve().parents[1] / "fixtures" / "caldor-2021"
NOW = dt.datetime(2021, 8, 15, 9, 30, tzinfo=dt.UTC)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    s = Store(tmp_path / "feed.duckdb")
    yield s
    s.close()


@pytest.fixture
def settings() -> Settings:
    return Settings()


Replay = Callable[..., list[RunSummary]]


@pytest.fixture
def replay(store: Store, settings: Settings, tmp_path: Path) -> Replay:
    """Runs the recorded scenario, or the first `upto` ticks of it."""

    def run(
        upto: int | None = None,
        repeat_last: bool = False,
        requests: list[str] | None = None,
        run_settings: Settings | None = None,
    ) -> list[RunSummary]:
        summaries = []
        chosen = fixtures.ticks(SCENARIO)[:upto]
        if repeat_last:
            chosen.append(chosen[-1])
        for tick in chosen:
            with httpx.Client(transport=fixtures.transport(SCENARIO, tick.name, requests)) as c:
                summaries.append(
                    run_tick(
                        run_settings or settings,
                        store,
                        c,
                        tmp_path / "public",
                        [FileSink(tmp_path / "public" / "notes.jsonl")],
                        now=tick.now,
                        sleep=lambda _: None,
                    )
                )
        return summaries

    return run
