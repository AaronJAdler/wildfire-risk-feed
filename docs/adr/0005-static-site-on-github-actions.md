# 5. Run on a GitHub Actions schedule and publish a static site to GitHub Pages

- Status: Accepted
- Date: 2026-11-07

## Context

The feed needs to run every few hours, keep a small amount of state, and be viewable by anyone.
It should cost nothing to keep running as a portfolio project.

## Decision

- A scheduled workflow (`live.yml`, every 3 hours and on demand) runs one tick.
- State (`state/feed.duckdb`: alerts, footprints, HTTP cache, history) is restored from and saved
  to the Actions cache. A cache miss starts fresh, which is safe: the feed rebuilds from the
  sources, at worst re-announcing open alerts once.
- Output is static: `feed.geojson`, `alerts.json`, `status.json` and a Leaflet page, deployed to
  GitHub Pages. The notifications file is not published.
- `FIRMS_MAP_KEY` is a repository secret. Without it the feed uses NASA's public 24-hour file.

## Consequences

- No server, database or bill. Anyone can consume the GeoJSON directly.
- Freshness is bounded by the schedule (3 hours here; GitHub may delay scheduled runs), which
  also bounds measured lead time. A deployment needing minutes would run the same code as a
  container on a scheduler with a real database.
- GitHub disables schedules in repositories with no activity for 60 days; a push or a manual
  run re-enables them.

## Alternatives considered

- **A small VM or container with cron**: more control, but costs money and needs patching.
- **A serverless function with object storage**: a good production path, and the code would not
  change; more setup than a portfolio project needs.
