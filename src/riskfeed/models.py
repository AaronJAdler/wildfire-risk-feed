"""The signals the feed combines, in a form every later step can trust."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Literal

SourceName = Literal["calfire", "wfigs", "firms", "nws"]
Health = Literal["ok", "stale", "down"]


@dataclass(frozen=True)
class Incident:
    """A fire an agency is tracking. The point is where it started, not its current extent."""

    id: str
    name: str
    lat: float
    lon: float
    acres: float | None
    contained: float | None  # percent
    started: dt.datetime | None
    updated: dt.datetime
    counties: tuple[str, ...] = ()
    url: str | None = None
    source: str = "calfire"  # calfire or wfigs
    kind: str = "wildfire"  # wildfire or prescribed
    agency: str | None = None


@dataclass(frozen=True)
class Perimeter:
    """A fire's mapped outline, as the resolution-7 cells it covers."""

    incident_id: str
    name: str
    acres: float | None
    cells: frozenset[str]


@dataclass(frozen=True)
class Hotspot:
    """One satellite detection of heat at the surface: a 375 m VIIRS pixel."""

    lat: float
    lon: float
    detected_at: dt.datetime
    satellite: str
    confidence: float  # 0-1
    frp: float  # fire radiative power, megawatts
    daynight: str

    @property
    def key(self) -> str:
        return f"{self.satellite}:{self.detected_at:%Y%m%dT%H%M}:{self.lat:.4f}:{self.lon:.4f}"


@dataclass(frozen=True)
class WeatherAlert:
    id: str
    event: str  # "Red Flag Warning", "Fire Weather Watch"
    headline: str
    onset: dt.datetime | None
    ends: dt.datetime | None
    cells: frozenset[str]
    zones: tuple[str, ...] = ()


@dataclass
class SourceResult:
    source: SourceName
    ok: bool
    records: list[Any] = field(default_factory=list)
    quarantined: list[dict[str, Any]] = field(default_factory=list)
    detail: str = ""
    skipped: bool = False  # circuit breaker open
    extra: Any = None  # source-specific extras: WFIGS perimeters
