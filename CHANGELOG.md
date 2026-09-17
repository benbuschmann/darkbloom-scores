# Changelog

## 0.2.1 — 2026-09-16

- Fix double-averaged scores: average raw requests / max(1, loaded) once over
  the selected window, then multiply by the observation's blended price and
  model weight. The default 30m score now matches the manager's method for
  identical samples, prices, and weights; spikes expire at the selected cutoff.
- Use corrected scores for the combined chart, model strips, headlines, and
  ranking. Explain the formula and raw sample coverage in the UI.
- Preserve stored history and the legacy `saved_score` for audit. Rows lacking
  raw counts or historical pricing show gaps, never inferred corrections.
- Return API schema 4 with the score method, raw/average pressure, sample count,
  price, and weight. Reject old cached score payloads until they expire.
- Keep count averages, independent controls, collection, retention, and caching
  unchanged. Add manager-equivalence and lingering-spike regression tests.

## 0.2.0 — 2026-09-16

- Default chart moving averages to 30 minutes, with an independent picker for
  15m, 30m, 1h, 2h, 4h, 12h, 24h, and 7d. Visible history still defaults to 2h.
- Apply the picker to loaded/request averages, utilization headlines, score
  curves, score strips, and score-based ranking; keep live requests unsmoothed.
- Smooth saved manager scores without rewriting source history or changing
  the manager formula. Return original values separately as `saved_score`.
- Read lookback before the visible chart, average by elapsed time before
  downsampling, and report separate partial count/score coverage.
- Keep the 30-second cache policy, with independent `(window, average)` keys
  and a 16-entry LRU limit. Stream averages to bound working memory.
- Retain 38 days of history to support a 7d average at the start of a 30d chart.
- Add the release version to the UI, API, health check, and image metadata.

## Previously unversioned

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
