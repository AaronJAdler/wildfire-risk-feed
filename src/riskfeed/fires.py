"""Which fire does each group of satellite detections belong to?

Detections are grouped by touching H3 cells at resolution 7 (about 2.5 km across). Each group is
then attributed, in order of preference:

1. To a CAL FIRE incident close enough to explain it. CAL FIRE's coordinates are where a fire
   started, so "close enough" grows with the fire's size. A fire listed without acreage, which
   is common for large federal fires, is treated as large.
2. To a fire whose recent footprint it touches: ground attributed to that fire in the last 72
   hours. Fires spread contiguously, so heat next to yesterday's heat is the same fire, even
   tens of kilometres from where it started.
3. To a neighbouring group that has already been attributed, repeated until nothing changes.

Groups left over are possible new fires. Published fire perimeters would make step 1 more
precise; the footprint makes the feed work without them.
"""

from __future__ import annotations

import datetime as dt
import math
from collections import Counter
from dataclasses import dataclass, field

from riskfeed import geo
from riskfeed.models import Hotspot, Incident

ACRE_KM2 = 0.00404686
UNKNOWN_SIZE_MATCH_KM = 25.0
REACH_RINGS = 2  # resolution-7 rings: about 3.5 km


@dataclass(frozen=True)
class Owner:
    """The fire a detection belongs to: a CAL FIRE incident id and its name."""

    id: str
    name: str


@dataclass
class Cluster:
    hotspots: list[Hotspot]
    cells: set[str] = field(default_factory=set)

    @property
    def lat(self) -> float:
        return sum(h.lat for h in self.hotspots) / len(self.hotspots)

    @property
    def lon(self) -> float:
        return sum(h.lon for h in self.hotspots) / len(self.hotspots)

    @property
    def first(self) -> dt.datetime:
        return min(h.detected_at for h in self.hotspots)

    @property
    def last(self) -> dt.datetime:
        return max(h.detected_at for h in self.hotspots)

    @property
    def peak_frp(self) -> float:
        return max(h.frp for h in self.hotspots)

    @property
    def qualifies(self) -> bool:
        """Enough heat to be a fire rather than an industrial site or a warm roof.

        Two or more detections that either carry real heat (5 MW or more) or were seen on more
        than one satellite pass; or one high-confidence detection of 10 MW or more. The first live
        run, in November, raised 29 alerts on 1-3 MW detections at refineries and city sites.
        """
        if any(h.confidence >= 0.95 and h.frp >= 10 for h in self.hotspots):
            return True
        passes = {
            h.detected_at.replace(minute=h.detected_at.minute // 30 * 30) for h in self.hotspots
        }
        return len(self.hotspots) >= 2 and (self.peak_frp >= 5 or len(passes) >= 2)


def clusters(hotspots: list[Hotspot], resolution: int) -> list[Cluster]:
    by_cell: dict[str, list[Hotspot]] = {}
    for h in hotspots:
        by_cell.setdefault(geo.cell(h.lat, h.lon, resolution), []).append(h)
    seen: set[str] = set()
    groups: list[Cluster] = []
    for start in sorted(by_cell):
        if start in seen:
            continue
        stack, members, cells = [start], [], set()
        seen.add(start)
        while stack:
            current = stack.pop()
            members.extend(by_cell[current])
            cells.add(current)
            for neighbour in geo.ring(current, 1):
                if neighbour in by_cell and neighbour not in seen:
                    seen.add(neighbour)
                    stack.append(neighbour)
        groups.append(Cluster(members, cells))
    return groups


def match_radius_km(incident: Incident, base_km: float) -> float:
    """Twice the radius of a circle with the fire's area, and never less than base_km."""
    if incident.acres is None:
        return max(base_km, UNKNOWN_SIZE_MATCH_KM)
    radius = math.sqrt(incident.acres * ACRE_KM2 / math.pi)
    return max(base_km, 2 * radius)


def explaining_incident(
    lat: float, lon: float, incidents: list[Incident], base_km: float
) -> tuple[Incident, float] | None:
    """The incident that best explains heat at this point, if any is close enough.

    "Best" is the smallest distance relative to each fire's match radius, so the front of a very
    large fire is credited to it and not to a small fire that happens to be nearer.
    """
    best: tuple[Incident, float, float] | None = None
    for i in incidents:
        distance = geo.haversine_km(lat, lon, i.lat, i.lon)
        relative = distance / match_radius_km(i, base_km)
        if relative <= 1 and (best is None or relative < best[2]):
            best = (i, distance, relative)
    return (best[0], best[1]) if best else None


def attribute(
    groups: list[Cluster], incidents: list[Incident], footprint: dict[str, Owner], base_km: float
) -> dict[int, Owner]:
    """Owner of each attributable group, by index into `groups`."""
    owners: dict[int, Owner] = {}
    for index, group in enumerate(groups):
        match = explaining_incident(group.lat, group.lon, incidents, base_km)
        if match:
            owners[index] = Owner(match[0].id, match[0].name)
            continue
        touched = Counter(
            footprint[c]
            for cell in group.cells
            for c in geo.ring(cell, REACH_RINGS)
            if c in footprint
        )
        if touched:
            owners[index] = sorted(touched.items(), key=lambda kv: (-kv[1], kv[0].id))[0][0]
    reach = {
        i: set().union(*(geo.ring(c, REACH_RINGS) for c in g.cells)) for i, g in enumerate(groups)
    }
    changed = True
    while changed:
        changed = False
        for index in range(len(groups)):
            if index in owners:
                continue
            neighbour = next((j for j in sorted(owners) if reach[index] & groups[j].cells), None)
            if neighbour is not None:
                owners[index] = owners[neighbour]
                changed = True
    return owners


def cell_owners(
    groups: list[Cluster], owners: dict[int, Owner], resolution: int
) -> dict[str, Owner]:
    """For each scoring cell, the fire most of its detections belong to."""
    votes: dict[str, Counter[Owner]] = {}
    for index, group in enumerate(groups):
        owner = owners.get(index)
        if owner is None:
            continue
        for h in group.hotspots:
            votes.setdefault(geo.cell(h.lat, h.lon, resolution), Counter())[owner] += 1
    return {c: sorted(v.items(), key=lambda kv: (-kv[1], kv[0].id))[0][0] for c, v in votes.items()}
