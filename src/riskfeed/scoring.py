"""A transparent 0-100 risk score per H3 cell.

This is a triage heuristic with every input visible, not a validated fire model. Three signals:

- fire: satellite detections in the last 24 hours, weighted by confidence and fire radiative
  power, halving in weight every 6 hours.
- incident: known CAL FIRE incidents near the cell, weighted by how uncontained and how large
  they are. Many federal fires are listed without acreage; those add a minimum weight and the
  satellite detections carry the rest.
- weather: a Red Flag Warning in force multiplies the score by 1.5, a Fire Weather Watch by 1.2,
  and on its own sets a floor ("elevated conditions, no fire seen").

score = 100 * (1 - exp(-(fire + incident) * weather / 6)), then at least the weather floor.
"""

from __future__ import annotations

import datetime as dt
import math
from collections import defaultdict
from dataclasses import dataclass, field

from riskfeed import geo
from riskfeed.models import Hotspot, Incident, WeatherAlert

SCALE = 6.0
WEATHER_MULTIPLIER = {"Red Flag Warning": 1.5, "Fire Weather Watch": 1.2}
WEATHER_FLOOR = {"Red Flag Warning": 25, "Fire Weather Watch": 15}
LEVELS = ((75, "extreme"), (50, "high"), (25, "moderate"), (0, "low"))
LEVEL_RANK = {"low": 0, "moderate": 1, "high": 2, "extreme": 3}


def level(score: int) -> str:
    return next(name for floor, name in LEVELS if score >= floor)


@dataclass
class CellScore:
    cell: str
    score: int
    level: str
    fire: float
    incident: float
    weather: float
    detections: int = 0
    peak_frp: float = 0.0
    incidents: list[str] = field(default_factory=list)
    weather_alerts: list[str] = field(default_factory=list)

    def reasons(self) -> list[str]:
        out = []
        if self.detections:
            out.append(
                f"{self.detections} satellite detections in 24 h, peak {self.peak_frp:.0f} MW"
            )
        out.extend(self.incidents)
        out.extend(self.weather_alerts)
        return out

    def as_row(self) -> dict[str, object]:
        return {
            "cell": self.cell,
            "score": self.score,
            "level": self.level,
            "fire": round(self.fire, 3),
            "incident": round(self.incident, 3),
            "weather": self.weather,
        }


def hotspot_weight(h: Hotspot, now: dt.datetime, half_life_hours: float) -> float:
    age_hours = max(0.0, (now - h.detected_at).total_seconds() / 3600)
    decay: float = 0.5 ** (age_hours / half_life_hours)
    return h.confidence * (1 + math.log10(1 + h.frp)) * decay


def incident_weight(i: Incident) -> float:
    uncontained = 1 - (i.contained if i.contained is not None else 0) / 100
    size = math.log10((i.acres if i.acres is not None else 10) + 10) / 2
    return uncontained * size


def incident_footprint(i: Incident, resolution: int) -> dict[str, float]:
    """The cell at the incident's origin, its neighbours at half weight, and for fires over
    10,000 acres a second ring at a quarter."""
    center = geo.cell(i.lat, i.lon, resolution)
    cells = {c: 0.5 for c in geo.ring(center, 1)}
    if (i.acres or 0) >= 10_000:
        cells.update({c: 0.25 for c in geo.ring(center, 2) - set(cells)})
    cells[center] = 1.0
    return cells


def score_cells(
    hotspots: list[Hotspot],
    incidents: list[Incident],
    weather: list[WeatherAlert],
    now: dt.datetime,
    resolution: int,
    half_life_hours: float,
) -> dict[str, CellScore]:
    fire: dict[str, float] = defaultdict(float)
    detections: dict[str, int] = defaultdict(int)
    peak: dict[str, float] = defaultdict(float)
    for h in hotspots:
        c = geo.cell(h.lat, h.lon, resolution)
        fire[c] += hotspot_weight(h, now, half_life_hours)
        detections[c] += 1
        peak[c] = max(peak[c], h.frp)

    incident: dict[str, float] = defaultdict(float)
    incident_notes: dict[str, list[str]] = defaultdict(list)
    for i in incidents:
        weight = incident_weight(i)
        acres = f"{i.acres:,.0f} acres" if i.acres is not None else "acreage not reported"
        contained = (
            f"{i.contained:.0f}% contained"
            if i.contained is not None
            else "containment not reported"
        )
        for c, share in incident_footprint(i, resolution).items():
            incident[c] += weight * share
            incident_notes[c].append(f"{i.name}: {acres}, {contained}")

    multiplier: dict[str, float] = defaultdict(lambda: 1.0)
    floor: dict[str, int] = defaultdict(int)
    weather_notes: dict[str, list[str]] = defaultdict(list)
    for a in weather:
        for c in a.cells:
            multiplier[c] = max(multiplier[c], WEATHER_MULTIPLIER.get(a.event, 1.0))
            floor[c] = max(floor[c], WEATHER_FLOOR.get(a.event, 0))
            until = f" until {a.ends:%b %d %H:%M} UTC" if a.ends else ""
            weather_notes[c].append(f"{a.event}{until}")

    scores: dict[str, CellScore] = {}
    for c in set(fire) | set(incident) | set(floor):
        raw = (fire[c] + incident[c]) * multiplier[c] / SCALE
        value = max(round(100 * (1 - math.exp(-raw))), floor[c])
        if value <= 0:
            continue
        scores[c] = CellScore(
            c,
            value,
            level(value),
            fire[c],
            incident[c],
            multiplier[c],
            detections[c],
            peak[c],
            sorted(set(incident_notes[c])),
            sorted(set(weather_notes[c])),
        )
    return scores
