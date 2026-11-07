"""National Weather Service fire weather alerts: Red Flag Warnings and Fire Weather Watches.

Most fire weather alerts carry no geometry. They name fire weather zones instead, and each
zone's outline comes from its own endpoint. Outlines rarely change, so conditional requests make
repeat lookups cheap.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from riskfeed.config import Settings
from riskfeed.geo import geometry_cells
from riskfeed.http import Fetcher
from riskfeed.models import SourceResult, WeatherAlert

FIRE_EVENTS = {"Red Flag Warning", "Fire Weather Watch"}
MAX_PAGES = 5


def _time(value: str | None) -> dt.datetime | None:
    return dt.datetime.fromisoformat(value) if value else None


def _zone_cells(fetcher: Fetcher, zone_url: str, resolution: int) -> set[str] | None:
    response = fetcher.get("nws", zone_url, accept="application/geo+json")
    if not response.ok or response.body is None:
        return None
    try:
        geometry = json.loads(response.body).get("geometry") or {}
    except ValueError:
        return None
    return geometry_cells(geometry, resolution)


def parse(
    features: list[dict[str, Any]], now: dt.datetime, fetcher: Fetcher | None, resolution: int
) -> tuple[list[WeatherAlert], list[str]]:
    """Alerts in force at `now`, and notes about anything that could not be placed on the map."""
    notes: list[str] = []
    replaced: set[str] = set()
    candidates: list[dict[str, Any]] = []
    for feature in features:
        props = feature.get("properties") or {}
        if props.get("event") not in FIRE_EVENTS or props.get("status") != "Actual":
            continue  # tests and exercises are published on the same feed
        for ref in props.get("references") or []:  # the alerts this one updates or cancels
            replaced.update({ref.get("@id", ""), ref.get("identifier", "")} - {""})
        if props.get("messageType") == "Cancel":
            continue
        candidates.append(feature)

    alerts: list[WeatherAlert] = []
    for feature in candidates:
        props = feature["properties"]
        alert_id = props.get("id") or feature.get("id", "")
        if alert_id in replaced or feature.get("id") in replaced:
            continue  # an update or cancellation supersedes it
        ends = _time(props.get("ends")) or _time(props.get("expires"))
        if ends is not None and ends <= now:
            continue
        zones = tuple(props.get("affectedZones") or [])
        if feature.get("geometry"):
            cells = geometry_cells(feature["geometry"], resolution)
        else:
            cells = set()
            for zone in zones:
                zone_cells = _zone_cells(fetcher, zone, resolution) if fetcher else None
                if zone_cells is None:
                    notes.append(f"no outline for {zone.rsplit('/', 1)[-1]}")
                else:
                    cells |= zone_cells
        alerts.append(
            WeatherAlert(
                alert_id,
                props["event"],
                props.get("headline") or "",
                _time(props.get("onset")),
                ends,
                frozenset(cells),
                tuple(z.rsplit("/", 1)[-1] for z in zones),
            )
        )
    return alerts, notes


def fetch(fetcher: Fetcher, settings: Settings, now: dt.datetime) -> SourceResult:
    features: list[dict[str, Any]] = []
    url: str | None = settings.nws_alerts_url
    pages = 0
    while url and pages < MAX_PAGES:
        response = fetcher.get("nws", url, accept="application/geo+json")
        if not response.ok or response.body is None:
            return SourceResult("nws", ok=False, detail=response.error or "no body")
        try:
            body = json.loads(response.body)
            features.extend(body["features"])
        except (ValueError, KeyError, TypeError) as exc:
            return SourceResult("nws", ok=False, detail=f"unreadable payload: {exc}"[:200])
        url = (body.get("pagination") or {}).get("next")
        pages += 1
    alerts, notes = parse(features, now, fetcher, settings.h3_resolution)
    detail = f"{len(alerts)} fire weather alerts in force"
    if notes:
        detail += "; " + "; ".join(sorted(set(notes)))
    return SourceResult("nws", ok=True, records=alerts, detail=detail)
