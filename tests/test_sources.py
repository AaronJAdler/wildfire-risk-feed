"""Parsing and validation of each source, including the ways they fail in practice."""

from __future__ import annotations

import datetime as dt
import json
import random

import httpx
import pytest

from riskfeed.config import Settings
from riskfeed.http import Fetcher
from riskfeed.sources import calfire, firms, nws
from riskfeed.store import Store

from .conftest import SCENARIO

TICK1 = SCENARIO / "ticks" / "1-satellites-first"
NOW = dt.datetime(2021, 8, 15, 21, 30, tzinfo=dt.UTC)


def _fetcher(store: Store, handler, settings: Settings | None = None) -> Fetcher:  # type: ignore[no-untyped-def]
    return Fetcher(
        httpx.Client(transport=httpx.MockTransport(handler)),
        store,
        settings or Settings(),
        "run",
        sleep=lambda _: None,
        rng=random.Random(0),
        clock=lambda: NOW,
    )


def test_calfire_quarantines_the_real_bad_longitude_and_skips_inactive() -> None:
    payload = json.loads((TICK1 / "calfire.json").read_text())
    incidents, quarantined = calfire.parse(payload)
    reasons = [q["reason"] for q in quarantined]
    assert any("-11846461" in r for r in reasons)  # Peak Fire, as published on 2021-07-20
    assert all(i.name for i in incidents)
    assert "Caldor Fire" not in {i.name for i in incidents}  # not listed yet on Aug 14
    active_in_feed = sum(1 for r in payload if r.get("Active") and not r.get("Final"))
    assert len(incidents) == active_in_feed - 1


def test_calfire_rejects_out_of_range_values() -> None:
    record = {
        "UniqueId": "x",
        "Name": "Test",
        "Latitude": 38.5,
        "Longitude": -120.5,
        "PercentContained": 140,
        "Updated": "2021-08-01T00:00:00Z",
        "Active": True,
    }
    incidents, quarantined = calfire.parse([record])
    assert not incidents and "PercentContained" in quarantined[0]["reason"]


def test_calfire_error_page_served_with_200_is_a_failure(store: Store) -> None:
    fetcher = _fetcher(store, lambda r: httpx.Response(200, text="<html>Maintenance</html>"))
    result = calfire.fetch(fetcher, Settings())
    assert not result.ok and "unreadable payload" in result.detail


def test_firms_maps_viirs_and_modis_confidence_and_quarantines_bad_rows() -> None:
    text = (TICK1 / "firms.csv").read_text()
    hotspots, quarantined, outside = firms.parse(text)
    assert {h.confidence for h in hotspots} <= {0.3, 0.7, 0.95}
    assert len(quarantined) == 1  # the row with confidence "?" and no FRP
    # The box is a coarse prefilter with a margin, so western Nevada passes and Oregon does not.
    assert outside == 2
    modis = (
        "latitude,longitude,acq_date,acq_time,satellite,confidence,frp,daynight\n"
        "38.6,-120.5,2021-08-15,0910,Terra,83,12.5,N\n"
    )
    (h,), _, _ = firms.parse(modis)
    assert h.confidence == 0.83 and h.detected_at == dt.datetime(2021, 8, 15, 9, 10, tzinfo=dt.UTC)


def test_firms_text_error_with_200_is_a_failure(store: Store) -> None:
    fetcher = _fetcher(store, lambda r: httpx.Response(200, text="Invalid MAP_KEY."))
    result = firms.fetch(fetcher, Settings())
    assert not result.ok and "not a FIRMS CSV" in result.detail


def test_firms_with_a_key_asks_each_satellite_and_survives_one_failing(store: Store) -> None:
    body = (TICK1 / "firms.csv").read_text()
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.path)
        return httpx.Response(503 if "NOAA21" in request.url.path else 200, text=body)

    result = firms.fetch(_fetcher(store, handler), Settings(firms_map_key="k3y"))
    assert len({p for p in asked}) == 3
    assert result.ok and "1 of 3 satellites failed" in result.detail


@pytest.fixture
def nws_features() -> list[dict]:
    tick2 = SCENARIO / "ticks" / "2-calfire-lists-caldor" / "nws_alerts.json"
    return json.loads(tick2.read_text())["features"]


def test_nws_update_supersedes_the_original_and_missing_zones_are_noted(
    store: Store, nws_features: list[dict]
) -> None:
    def zones(request: httpx.Request) -> httpx.Response:
        code = request.url.path.rsplit("/", 1)[-1]
        path = SCENARIO / "zones" / f"{code}.json"
        return (
            httpx.Response(200, content=path.read_bytes()) if path.exists() else httpx.Response(404)
        )

    alerts, notes = nws.parse(nws_features, NOW, _fetcher(store, zones), 6)
    events = sorted(a.event for a in alerts)
    assert events == ["Fire Weather Watch", "Red Flag Warning"]
    rfw = next(a for a in alerts if a.event == "Red Flag Warning")
    assert rfw.id.endswith("rfw.2") and rfw.cells  # cells come from the zone outlines
    assert notes == ["no outline for CAZ903"]


def test_nws_drops_tests_other_events_expired_and_cancelled(store: Store) -> None:
    tick1 = json.loads((TICK1 / "nws_alerts.json").read_text())["features"]
    alerts, _ = nws.parse(tick1, dt.datetime(2021, 8, 15, 9, 30, tzinfo=dt.UTC), None, 6)
    assert {a.event for a in alerts} == {"Red Flag Warning", "Fire Weather Watch"}  # no test,
    assert all(a.headline for a in alerts)  # no heat advisory
    tick3 = SCENARIO / "ticks" / "3-firms-outage" / "nws_alerts.json"
    later, _ = nws.parse(
        json.loads(tick3.read_text())["features"],
        dt.datetime(2021, 8, 16, 21, 30, tzinfo=dt.UTC),
        None,
        6,
    )
    assert later == []  # the warning expired and the watch was cancelled


def test_nws_follows_pagination(store: Store) -> None:
    pages = {
        "/alerts/active": {
            "features": [],
            "pagination": {"next": "https://api.weather.gov/alerts/active?cursor=2"},
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if "cursor=2" in str(request.url):
            return httpx.Response(200, json={"features": []})
        return httpx.Response(200, json=pages["/alerts/active"])

    result = nws.fetch(_fetcher(store, handler), Settings(), NOW)
    assert result.ok
    paths = store.db.execute("SELECT url FROM fetch_log ORDER BY logged_at").fetchall()
    assert len(paths) == 2
