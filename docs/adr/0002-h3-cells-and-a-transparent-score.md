# 2. H3 hexagons and a transparent heuristic score

- Status: Accepted
- Date: 2026-11-07

## Context

Three signals with different shapes have to be combined: points (detections, incident origins)
and polygons (weather zones). Consumers want one map and one number per area, and responders need
to see why an area scores high.

## Decision

- The unit of area is an H3 cell at resolution 6 (about 36 km²). Points map to the cell that
  contains them; polygons to the cells whose centres they contain.
- The score is 100 x (1 - exp(-(fire + incident) x weather / 6)), plus a floor when a weather
  alert is in force:
  - fire: per detection, confidence x (1 + log10(1 + FRP)), halving every 6 hours;
  - incident: (1 - containment) x log10(acres + 10) / 2, spread to neighbouring cells;
  - weather: x1.5 for a Red Flag Warning (floor 25), x1.2 for a Fire Weather Watch (floor 15).
- Every cell in the feed carries the reasons behind its score.
- Levels: low below 25, moderate below 50, high below 75, extreme from 75.

## Consequences

- Equal-area hexagons make neighbours, rings and joins simple and fast, without a GIS stack.
- The score is monotonic in the right directions, and tests check it: more heat never lowers
  it, containment lowers it, age lowers it, a Red Flag Warning raises it.
- It is a triage heuristic, not a validated fire-behaviour model, and the docs and page say so.
  Fuel moisture, terrain and wind speed are not inputs.

## Alternatives considered

- **Counties**: familiar, but wildly different sizes (San Bernardino vs San Francisco).
- **A trained model**: needs labelled outcomes, and an opaque number is worse for triage than an
  explained one.
- **PostGIS or shapely polygons throughout**: more precise, and heavier than the problem needs.
