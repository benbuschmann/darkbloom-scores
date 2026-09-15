# Changelog

## Unreleased

- Add per-model charts below the combined score chart, ranked by latest score,
  with 15-minute loaded/request averages, a toggleable light-gray live request
  line, and a toggleable saved-score strip. All charts share the eight windows.
- Save public count history using an additive migration; preserve existing
  scores and leave unknown historical counts null. Compute count averages over
  900 elapsed seconds with explicit partial coverage and collection-gap handling.
- Extend the existing cached score response instead of adding more requests;
  preserve the deployed application/Cloudflare cache behavior and score formula.
- Add count migration, elapsed-average, cache, and frontend regression tests.
- Remove public traffic cards and all custom visitor-IP/session collection,
  heartbeat writes, traffic API/reporting, and proxy-attribution settings.
- Retire the legacy visitor table on startup while preserving score history.
- Permit Cloudflare's automatically injected Web Analytics beacon and document
  its separate dashboard setup. No analytics credential is needed by the app.
- Keep model collection, scoring, chart windows, and minute refresh unchanged.
