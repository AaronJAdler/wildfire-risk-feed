"""NASA FIRMS active fire detections from the VIIRS instruments.

With a MAP_KEY, the area API returns California only, for each satellite. Without one, the feed
falls back to NASA's public 24-hour file for the lower 48 states and filters it to California.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from typing import Any

from riskfeed.config import CA_BBOX, FIRMS_SATELLITES, Settings
from riskfeed.geo import in_bbox
from riskfeed.http import Fetcher
from riskfeed.models import Hotspot, SourceResult

# VIIRS reports confidence as low / nominal / high; MODIS as 0-100.
_VIIRS_CONFIDENCE = {"l": 0.3, "low": 0.3, "n": 0.7, "nominal": 0.7, "h": 0.95, "high": 0.95}


def _confidence(value: str) -> float:
    value = value.strip().lower()
    if value in _VIIRS_CONFIDENCE:
        return _VIIRS_CONFIDENCE[value]
    number = float(value)
    if not 0 <= number <= 100:
        raise ValueError(f"confidence {value} out of range")
    return number / 100


def parse(text: str) -> tuple[list[Hotspot], list[dict[str, Any]], int]:
    """Hotspots in California, malformed rows with reasons, and the count outside the area."""
    header = text.lstrip().split("\n", 1)[0].lower()
    if "latitude" not in header or "longitude" not in header:
        # FIRMS answers a bad key or an overload with a plain-text message and status 200.
        raise ValueError(f"not a FIRMS CSV: {text.strip()[:80]!r}")
    hotspots: list[Hotspot] = []
    quarantined: list[dict[str, Any]] = []
    outside = 0
    for row in csv.DictReader(io.StringIO(text)):
        try:
            lat, lon = float(row["latitude"]), float(row["longitude"])
            hhmm = row["acq_time"].strip().zfill(4)
            detected = dt.datetime.strptime(f"{row['acq_date']} {hhmm}", "%Y-%m-%d %H%M").replace(
                tzinfo=dt.UTC
            )
            hotspot = Hotspot(
                lat,
                lon,
                detected,
                row.get("satellite", "?").strip(),
                _confidence(row["confidence"]),
                max(0.0, float(row["frp"] or 0)),
                row.get("daynight", "?").strip(),
            )
        except (KeyError, ValueError, TypeError) as exc:
            quarantined.append({"reason": f"{type(exc).__name__}: {exc}", "record": row})
            continue
        if not in_bbox(lat, lon, CA_BBOX):
            outside += 1
            continue
        hotspots.append(hotspot)
    return hotspots, quarantined, outside


def fetch(fetcher: Fetcher, settings: Settings) -> SourceResult:
    if settings.firms_map_key:
        bbox = ",".join(str(v) for v in CA_BBOX)
        urls = [
            settings.firms_area_url.format(key=settings.firms_map_key, satellite=s, bbox=bbox)
            for s in FIRMS_SATELLITES
        ]
    else:
        urls = [settings.firms_public_url]
    hotspots: list[Hotspot] = []
    quarantined: list[dict[str, Any]] = []
    failures: list[str] = []
    for url in urls:
        response = fetcher.get("firms", url, accept="text/csv")
        if not response.ok or response.body is None:
            failures.append(response.error or "no body")
            continue
        try:
            found, bad, _ = parse(response.body.decode("utf-8", errors="replace"))
        except ValueError as exc:
            failures.append(str(exc))
            continue
        hotspots.extend(found)
        quarantined.extend(bad)
    if len(failures) == len(urls):
        return SourceResult("firms", ok=False, detail="; ".join(failures)[:200])
    mode = "area API" if settings.firms_map_key else "public 24h file"
    detail = f"{len(hotspots)} detections via {mode}"
    if failures:
        detail += f"; {len(failures)} of {len(urls)} satellites failed"
    return SourceResult("firms", ok=True, records=hotspots, quarantined=quarantined, detail=detail)
