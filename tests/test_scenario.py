"""The recorded Caldor Fire scenario end to end, plus alert, breaker and delivery behaviour."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import httpx

from riskfeed.config import Settings
from riskfeed.delivery import FileSink, WebhookSink, deliver
from riskfeed.store import Store

from .conftest import Replay


def _names(summary) -> list[str]:  # type: ignore[no-untyped-def]
    return sorted(f"{t.name}: {t.title}" for t in summary.transitions)


def test_satellites_see_caldor_first_and_cal_fire_confirms_it(replay: Replay, store: Store) -> None:
    first, second = replay(upto=2)
    assert "raised: Possible new fire" in _names(first)
    assert "confirmed: Confirmed: Caldor Fire" in _names(second)
    confirmed = next(a for a in store._rows("SELECT key FROM alerts WHERE level = 'confirmed'"))
    details = store.alert(confirmed["key"])["details"]  # type: ignore[index]
    assert details["incident"] == "Caldor Fire" and details["lead_minutes"] == 720


def test_a_short_burn_expires_without_an_incident(replay: Replay) -> None:
    _, second = replay(upto=2)
    assert "expired: Possible new fire: no further heat" in _names(second)


def test_an_outage_never_clears_alerts_and_recovery_does(replay: Replay, store: Store) -> None:
    *_, outage, recovery = replay()
    assert outage.completeness == "partial" and outage.sources["firms"]["health"] == "down"
    assert outage.transitions == []
    monument = store.alert(
        "risk:incident:" + next(i.id for i in store.active_incidents() if i.name == "Monument Fire")
    )
    assert monument and monument["status"] == "closed"
    assert recovery.completeness == "complete"
    assert "cleared: Fire risk eased: Monument Fire" in _names(recovery)


def test_the_held_back_clear_is_visible_during_the_outage(replay: Replay, tmp_path: Path) -> None:
    replay(upto=3)
    alerts = json.loads((tmp_path / "public" / "alerts.json").read_text())
    assert alerts["completeness"] == "partial"
    assert any("Not cleared yet" in a["text"] and "firms" in a["text"] for a in alerts["open"])


def test_rerunning_a_tick_sends_nothing_new(replay: Replay, tmp_path: Path) -> None:
    runs = replay(upto=2, repeat_last=True)
    assert runs[-1].transitions == []
    lines = (tmp_path / "public" / "notes.jsonl").read_text().splitlines()
    ids = [json.loads(line)["id"] for line in lines]
    assert len(ids) == len(set(ids))


def test_one_fire_is_one_alert_not_one_per_hexagon(replay: Replay) -> None:
    (first,) = replay(upto=1)
    risk = [t for t in first.transitions if t.kind == "risk"]
    assert len(risk) == len({t.key for t in risk}) <= 4


def test_feed_files_say_how_complete_the_run_was(replay: Replay, tmp_path: Path) -> None:
    replay(upto=1)
    feed = json.loads((tmp_path / "public" / "feed.geojson").read_text())
    assert feed["completeness"] == "complete" and feed["features"]
    top = feed["features"][0]
    assert top["geometry"]["type"] == "Polygon" and top["properties"]["reasons"]
    status = json.loads((tmp_path / "public" / "status.json").read_text())
    assert status["sources"]["calfire"]["quarantined"] == 1  # the bad Peak Fire record
    assert status["sources"]["firms"]["quarantined"] == 1


def test_the_circuit_opens_after_repeated_failures(replay: Replay, store: Store) -> None:
    requests: list[str] = []
    settings = Settings(breaker_threshold=1)
    replay(upto=3, requests=requests, run_settings=settings)  # tick 3: FIRMS answers 503
    assert store.breaker("firms")[1] is not None
    before = sum("firms" in r for r in requests)
    second = replay(upto=3, requests=requests, run_settings=settings)  # inside the open window
    assert sum("firms" in r for r in requests) == before  # FIRMS was not called at all
    assert all("circuit open" in run.sources["firms"]["detail"] for run in second)


def test_a_failing_webhook_keeps_the_message_for_the_next_run(store: Store, tmp_path: Path) -> None:
    now = dt.datetime(2021, 8, 15, tzinfo=dt.UTC)
    store.enqueue(
        "risk:x:v1", "risk:x", "raised", {"id": "risk:x:v1", "title": "t", "text": "x"}, now
    )
    keys: list[str] = []

    def flaky(request: httpx.Request) -> httpx.Response:
        keys.append(request.headers["Idempotency-Key"])
        return httpx.Response(500 if len(keys) == 1 else 200)

    sinks = [
        FileSink(tmp_path / "n.jsonl"),
        WebhookSink(
            "https://hooks.example.test/x", httpx.Client(transport=httpx.MockTransport(flaky))
        ),
    ]
    assert deliver(store, sinks, now) == {"sent": 0, "failed": 1}
    assert deliver(store, sinks, now) == {"sent": 1, "failed": 0}
    assert keys == ["risk:x:v1", "risk:x:v1"]
    assert store.pending() == []
