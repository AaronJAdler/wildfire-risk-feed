"""One run of the feed: fetch, score, alert, deliver, publish.

Each source is fetched independently. A failure in one never stops the run: the feed is
published from the best data available, marked partial, and alerts that would need the
missing data to change state are left as they are.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from riskfeed import alerts, fires, scoring
from riskfeed.config import Settings
from riskfeed.delivery import Sink, deliver
from riskfeed.http import Fetcher
from riskfeed.models import Health, SourceName, SourceResult
from riskfeed.publish import publish
from riskfeed.sources import calfire, firms, nws
from riskfeed.store import Store

log = logging.getLogger(__name__)
SOURCES: tuple[SourceName, ...] = ("calfire", "firms", "nws")
FOOTPRINT_MEMORY = dt.timedelta(hours=72)


@dataclass
class RunSummary:
    run_id: str
    at: dt.datetime
    completeness: str
    sources: dict[str, dict[str, Any]]
    levels: dict[str, int]
    transitions: list[alerts.Transition] = field(default_factory=list)
    delivery: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "at": self.at.isoformat(),
            "completeness": self.completeness,
            "sources": self.sources,
            "levels": self.levels,
            "delivery": self.delivery,
            "transitions": [f"{t.name}: {t.title} ({t.key})" for t in self.transitions],
        }


def _fetch_source(
    name: SourceName, fetcher: Fetcher, settings: Settings, now: dt.datetime
) -> SourceResult:
    if name == "calfire":
        return calfire.fetch(fetcher, settings)
    if name == "firms":
        return firms.fetch(fetcher, settings)
    return nws.fetch(fetcher, settings, now)


def _collect(
    name: SourceName,
    store: Store,
    fetcher: Fetcher,
    settings: Settings,
    run_id: str,
    now: dt.datetime,
) -> SourceResult:
    failures, open_until = store.breaker(name)
    if open_until and now < open_until:
        return SourceResult(
            name, ok=False, skipped=True, detail=f"circuit open until {open_until:%H:%M} UTC"
        )
    try:
        result = _fetch_source(name, fetcher, settings, now)
    except Exception as exc:  # noqa: BLE001 - one broken adapter must not stop the others
        log.exception("%s adapter crashed", name)
        result = SourceResult(name, ok=False, detail=f"adapter error: {type(exc).__name__}")
    with store.transaction():
        if result.ok:
            if name == "calfire":
                store.replace_incidents(result.records, now)
            elif name == "firms":
                store.add_hotspots(result.records, now)
            else:
                store.replace_weather(result.records)
            store.quarantine(run_id, name, result.quarantined, now)
            store.mark_success(name, now, len(result.records), len(result.quarantined))
            store.set_breaker(name, 0, None)
        else:
            failures += 1
            tripped = failures >= settings.breaker_threshold
            store.set_breaker(name, failures, now + settings.breaker_cooldown if tripped else None)
            if tripped:
                result.detail += f"; circuit opened after {failures} failed runs"
    return result


def health_of(
    name: SourceName, result: SourceResult, store: Store, settings: Settings, now: dt.datetime
) -> Health:
    if result.ok:
        return "ok"
    last = store.last_success(name)
    return "stale" if last and now - last <= settings.max_age[name] else "down"


def run_tick(
    settings: Settings,
    store: Store,
    client: httpx.Client,
    out_dir: str | Path,
    sinks: list[Sink],
    now: dt.datetime | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> RunSummary:
    now = now or dt.datetime.now(dt.UTC)
    run_id = now.strftime("%Y%m%dT%H%M%SZ")
    fetcher = Fetcher(client, store, settings, run_id, sleep=sleep, clock=lambda: now)

    results = {name: _collect(name, store, fetcher, settings, run_id, now) for name in SOURCES}
    health: dict[str, Health] = {
        name: health_of(name, results[name], store, settings, now) for name in SOURCES
    }
    completeness = "complete" if all(h == "ok" for h in health.values()) else "partial"
    sources: dict[str, dict[str, Any]] = {
        name: {
            "health": health[name],
            "detail": results[name].detail,
            "quarantined": len(results[name].quarantined),
            "last_success": (seen.isoformat() if (seen := store.last_success(name)) else None),
        }
        for name in SOURCES
    }

    # The best data available: last known incidents and weather, and stored detections.
    incidents = store.active_incidents()
    weather = [
        a
        for a in store.weather_alerts()
        if (a.onset is None or a.onset <= now) and (a.ends is None or a.ends > now)
    ]
    hotspots = store.hotspots_between(now - settings.hotspot_window, now)
    recent = [h for h in hotspots if h.detected_at > now - settings.ignition_window]

    scores = scoring.score_cells(
        hotspots, incidents, weather, now, settings.h3_resolution, settings.hotspot_half_life_hours
    )
    groups = fires.clusters(hotspots, settings.ignition_cluster_resolution)
    owners = fires.attribute(
        groups, incidents, store.footprint(now - FOOTPRINT_MEMORY), settings.ignition_match_km
    )
    candidates = [
        g
        for i, g in enumerate(groups)
        if i not in owners and g.qualifies and g.last > now - settings.ignition_window
    ]
    with store.transaction():
        store.update_footprint(
            {
                cell_id: (owner, groups[i].last)
                for i, owner in owners.items()
                for cell_id in groups[i].cells
            },
            forget_before=now - 7 * FOOTPRINT_MEMORY,
        )
        transitions = alerts.evaluate(
            store,
            now,
            scores,
            fires.cell_owners(groups, owners, settings.h3_resolution),
            candidates,
            incidents,
            recent,
            health,
            settings,
        )
        store.record_run(run_id, now, completeness, sources, [s.as_row() for s in scores.values()])
    delivery = deliver(store, sinks, now)
    publish(
        out_dir,
        now,
        run_id,
        completeness,
        sources,
        scores,
        store.open_alerts(),
        store.latest_messages(),
    )
    levels: dict[str, int] = {}
    for s in scores.values():
        levels[s.level] = levels.get(s.level, 0) + 1
    summary = RunSummary(run_id, now, completeness, sources, levels, transitions, delivery)
    log.info(
        "run %s %s: %d cells, %d transitions", run_id, completeness, len(scores), len(transitions)
    )
    return summary
