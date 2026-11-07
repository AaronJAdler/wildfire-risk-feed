# 7. Add NIFC WFIGS incidents and perimeters alongside CAL FIRE

- Status: Accepted
- Date: 2026-11-07

## Context

After the feed went live, CAL FIRE's active incident list returned an empty array, while CAL
FIRE's own incident map showed hundreds of fires. A probe run in GitHub Actions found why:

- The map shows `List?inactive=true`: 567 fires from 2026, all contained.
- CAL FIRE's list covers fires CAL FIRE reports on. NIFC's interagency WFIGS service, which every
  agency feeds, had 69 current California incidents: 39 wildfires (Forest Service, BLM, Park
  Service, local agencies, CAL FIRE) and 30 prescribed burns.
- WFIGS also publishes mapped perimeters.

## Decision

- WFIGS current incident locations are a fourth source, with the same health, breaker and
  validation rules as the others. ArcGIS errors served with status 200 count as failures.
- A WFIGS incident with the same normalised name as a CAL FIRE incident within 10 km is the same
  fire: CAL FIRE's record is kept and the WFIGS id becomes an alias, so its perimeter still
  applies.
- WFIGS perimeters become the first attribution step: heat inside or touching a fire's outline
  belongs to that fire, wherever its origin point is.
- Prescribed burns explain heat (no "possible new fire") and are shown on the map, but never
  raise risk alerts.
- The map gains toggleable layers: active incidents (wildfire or prescribed), perimeters, and
  this year's contained fires from CAL FIRE's `inactive=true` list. Contained fires never
  affect scores.

## Consequences

- The feed sees federal and local fires CAL FIRE does not list, and planned burns stop looking
  like new fires: in November 2026 these were most of the heat in the state.
- Perimeters fix most of the mis-attribution described in ADR 0003 for fires that have one.
- A fourth source is one more way for a run to be `partial`, which holds back clears.

## Alternatives considered

- **Only CAL FIRE**: misses most incidents outside CAL FIRE's own reporting.
- **Only WFIGS**: loses CAL FIRE's public incident pages and acreage updates for its fires.
