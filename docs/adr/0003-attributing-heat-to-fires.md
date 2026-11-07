# 3. Attribute detections to fires with size-aware matching and a 72-hour footprint

- Status: Accepted
- Date: 2026-11-07

## Context

The most useful alert this feed can send is "satellites see a fire nobody has reported". That
needs to know which detections belong to known fires. CAL FIRE gives only each fire's origin
point, often no acreage for large federal fires, and its timestamps carry a `Z` while appearing
to be Pacific time (Caldor's `18:54Z` matches its reported 6:54 p.m. PDT start).

The first version matched detections to incidents within 10 km. Replaying August 2021 exposed two
failures: one alert per hexagon (Dixie alone raised 11), and Caldor's spread raising four false
"possible new fire" alerts within two days.

## Decision

- Group detections by touching resolution-7 cells.
- Attribute a group to the incident that best explains it: within twice the radius of a circle
  with the fire's area (at least 10 km, or 25 km when acreage is unreported), ranked by distance
  relative to that radius, so a large fire's front is not credited to a small fire nearby.
- Otherwise attribute it to the fire whose footprint it touches: cells attributed to that fire
  in the last 72 hours. Then spread attribution between touching groups until stable.
- Risk alerts are per fire (`risk:incident:<id>`), summarising every cell attributed to it.
  Unattributed heat is left to "possible new fire" alerts, so one event never pages twice.
- Lead time is measured from the alert to this feed's first sighting of the incident in CAL
  FIRE's list, not from CAL FIRE's own timestamps.

- Not every unattributed group is a candidate. The first live run (November 2026) raised 29
  "possible new fire" alerts, nearly all 1-3 MW detections at refineries and city sites, plus
  Reno and Las Vegas inside the bounding box. A candidate now needs real heat (two detections and
  5 MW, or two satellite passes, or one high-confidence 10 MW detection), must be inside a
  California outline, and must not be a persistent heat source: a place with detections on 3 or
  more days in the last 30. Open alerts that a filter now rejects are closed as `dismissed`.

## Consequences

- The replay now raises one alert per fire, and no false new-fire alerts as Caldor grows.
- A genuinely new fire that starts within about 3.5 km of an active fire's footprint is treated
  as part of that fire. Operationally that is usually right (spot fires).
- Where two listed fires sit close together, heat can still go to the wrong one; the replay
  credits part of the Dixie front to the Evans Fire. Published perimeters (NIFC WFIGS) would
  fix this and are the next step.

## Alternatives considered

- **Fixed radius**: the false alerts above.
- **Perimeters only**: accurate but published hours to days late, exactly when the feed is most
  useful.
- **Per-cell alerts**: noise that teaches people to ignore the channel.
