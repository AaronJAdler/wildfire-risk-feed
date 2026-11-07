# 1. Degrade per source, and never let missing data clear an alert

- Status: Accepted
- Date: 2026-11-07

## Context

The feed depends on three public services that fail independently: NASA FIRMS answers 503 under
load, CAL FIRE's site has maintenance pages served with status 200, and NWS rate-limits. When a
source is missing, scores fall: no detections means no fire component. A naive pipeline would
then send "risk eased" for a fire that is still burning.

## Decision

- Each source is fetched, validated and stored independently. One failure never stops the run.
- Each source ends a run as `ok`, `stale` (failed, but last success within 6 h for CAL FIRE,
  12 h for FIRMS, 2 h for NWS) or `down`. The run is `partial` unless all three are `ok`, and
  every published file says so.
- Scores always use the best data available: last known incidents and weather, stored
  detections with their normal decay.
- Transitions that mean "things got better" require the data that would show it: clearing a risk
  alert needs a complete run, expiring a possible new fire needs fresh FIRMS, confirming one
  needs fresh CAL FIRE. Raising a possible new fire needs CAL FIRE not `down`, or every known
  fire would look new.
- A held-back clear is recorded on the alert and shown on the page ("Not cleared yet: firms not
  current").
- A source that fails 3 runs in a row is skipped for 30 minutes (circuit breaker), so the feed
  stops hammering a service that is down.

## Consequences

- In the replay, FIRMS fails for a whole day; every score drops, and no alert clears. When FIRMS
  returns, Monument (actually quiet) clears and Caldor (still burning) does not.
- Alerts can stay open longer than necessary during an outage. That is the right error for a
  safety feed: a late "all clear" costs less than a false one.
- Consumers can tell "no risk" from "no data" by the `completeness` field and per-source health.

## Alternatives considered

- **Fail the whole run when any source fails**: simple, but one flaky API takes down the feed.
- **Treat missing data as zero**: produces false all-clears.
- **Freeze the whole feed during an outage**: hides real new heat from the sources that work.
