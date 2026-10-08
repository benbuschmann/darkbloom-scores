# Darkbloom Scores

Current release: **v0.2.1**.

A small public website comparing all Darkbloom model scores over time. One
Python service collects public capacity and pricing, saves SQLite history, and
serves plain HTML/SVG charts. No API key, paid database, provider installation,
or running Mac is needed.

**Deploying with Dokploy? Start with [DEPLOY_DOKPLOY.md](DEPLOY_DOKPLOY.md).**

## Features and scope

- Combined score chart with abbreviated, clickable endpoint labels.
- Per-model charts ranked by latest pressure score: green loaded-model and blue
  request averages, a toggleable light-gray live request line, and a toggleable
  pressure-score strip. Each card shows its latest average utilization and score.
- 30m, 1h, **2h default**, 4h, 12h, 24h, 7d, and 30d chart windows.
- Independent moving-average picker: 15m, **30m default**, 1h, 2h, 4h, 12h,
  24h, or 7d. Applies to counts and score charts; live requests stay unsmoothed.
- One cached response shared by all charts, keyed by both controls.
- Minute collection; one pressure average, using the manager's score formula.
- 38-day public score/count retention, including averaging lookback;
  visitor analytics handled separately by Cloudflare.
- Dockerfile, offline tests, GitHub Actions image and persistence checks.

This is only the public model-chart website. It does **not** include the private tracker,
personal token earnings, fleet machine identities, existing databases, or any
credentials. The repo starts without historical data. Collection begins when
deployed; earlier windows stay empty unless public score history is imported
separately.

Existing deployments keep their saved history. Corrected scores are computed
from recorded raw counts, price, and weight without rewriting the database.
Older score-only records remain stored but cannot produce corrected chart
scores; they appear as gaps, not zero. A full average needs that much recorded
history; partial coverage is labeled separately for counts and pressure.

## Scoring

```text
pressure = active_requests / max(1, warm_providers)
blended_price = 0.85 × input_price + 0.15 × output_price
score = mean(pressure samples in the selected window) × blended_price × model_weight
```

The default is **30 minutes**: take the arithmetic mean of raw minute pressure
samples in `(timestamp − 1800 seconds, timestamp]`, including the current sample,
with at most 30 samples. Other selected windows use the same method, not simply
the last N observations regardless of age. This matches the manager for the
same samples, price, and weight; different sampling times can differ slightly.
Startup/gaps use only available samples; missing history is not invented.
Multiply only after averaging pressure, using the price and weight recorded at
each plotted timestamp. Do not average already-smoothed scores or reprice old
history using today's price. For example, `0.007875 × 0.08225 × 1 = 0.00064771875`
(the combined chart's three-decimal label is `0.001`).
Prices refresh every 15 minutes. On pricing errors, cached prices remain in
use with a warning; no usable price means a null score.

Weights and endpoint parsing come from a checksum-pinned
[manager dependency](THIRD_PARTY.md). The score is a comparison signal, **not
actual revenue, dollars per hour, or a performance benchmark**.

Public sources, accessed without credentials:

- `https://console.darkbloom.dev/api/models/capacity`
- `https://api.darkbloom.dev/v1/pricing`

### Independent chart window and moving average

**Show history** sets the visible horizontal time range (2h by default).
**Moving average** sets the trailing smoothing duration (30m by default),
independently. A 7d average on a 2h chart reads the preceding seven days for
every plotted point; it does not average only the visible two hours.

The server averages raw counts over elapsed seconds before display downsampling.
Each count observation is held until the next sample; intervals longer than
150 seconds are excluded as collection gaps. Score pressure instead uses the
manager's arithmetic sample mean described above. Count time coverage and
pressure sample coverage are labeled separately. A first observation is
provisional; unknown values remain gaps, not zeroes.

The combined chart, amber strip, card score, and sort order all use the same
single pressure average multiplied by price and weight. The headline percentage
is average requests divided by average loaded models, not the mean of individual
sample percentages. Count averaging is unchanged by the score fix.

For compatibility/audit, the collector still saves its original 15-minute
manager score and returns it as `saved_score`. That legacy value is **never**
input to the displayed score calculation. v0.2.0 incorrectly averaged those
saved scores a second time; v0.2.1 corrects this without rewriting history.

Green, blue, and gray share one count scale. The right-hand green reference is
100%; blue can cross above it. Gray is the latest **minute-sampled** request
count, not a continuous feed; its right-hand percentage also uses the latest
average loaded count so the labels match the common scale. Zero loaded capacity
shows an unknown percentage rather than infinity. The amber score strip has its
own scale and the same timeline. Toggle choices survive refreshes/control changes
within the page, without storing any visitor identifiers.

## Run locally

Python 3.10+ on macOS/Linux; Docker uses Python 3.12. No pip packages.

```sh
python3 fetch_manager.py
python3 -m unittest -v
node --test test_frontend.js
python3 server.py serve
```

Open <http://127.0.0.1:8788/>. Stop with Ctrl-C. This does not touch an existing
local tracker on port 8787. Data is saved to `data/scores.sqlite3`, ignored by
Git. First setup downloads and verifies the exact public dependency; subsequent
verified setups can run offline.

## Docker

```sh
docker build -t darkbloom-scores .
docker volume create darkbloom-scores-data
docker run -d --name darkbloom-scores --restart unless-stopped \
  -p 127.0.0.1:8788:8788 \
  -v darkbloom-scores-data:/data \
  darkbloom-scores
```

For public hosting use Dokploy's reverse proxy, not a directly published
container port. Run **one replica** with a persistent volume at `/data`. The
process runs as UID/GID `10001:10001`; that directory must be writable by it.
Preserve the volume during redeployment.

## Configuration

| Variable | Local default | Docker default / purpose |
| --- | --- | --- |
| `BIND_HOST` | `127.0.0.1` | `0.0.0.0` |
| `PORT` | `8788` | `8788` |
| `SCORES_DB` | `data/scores.sqlite3` | `/data/scores.sqlite3` |
| `POLL_INTERVAL_SECONDS` | `60` | Minimum 60; larger values make averages sparser |

No `.env` or Darkbloom key is required. Set values in Dokploy, never commit
credentials. CLI flags override corresponding environment variables; see
`python3 server.py serve --help`.

## HTTP API

- `GET /` — chart page.
- `GET /healthz` — process liveness and release version, not upstream freshness.
- `GET /api/scores?window=7200&average=1800` — chart data and freshness timestamps.
- Old traffic endpoints and all POST requests return 404 without storing data.

Only the chart's supported durations in seconds are accepted. Up to 12h, the
API returns minute points; 24h uses 2-minute buckets, 7d uses 15-minute buckets,
and 30d uses hourly buckets. Each bucket returns its **last** already-averaged
point, not another average. Averages are computed from the original minute
observations plus lookback, never from downsampled points. The UI marks the feed delayed
after three minutes.

API schema version 4 includes the selected `average_seconds`, app `version`,
and `score_method: "mean_pressure_times_price_weight"`. Each row includes raw
`loaded`, `requests`, `available_to_load`, `pressure`, and legacy `saved_score`,
alongside `average_loaded`, `average_requests`, `average_pressure`, corrected
`score`, `blended_price_usd`, `model_weight`, and `pressure_sample_count`.
Count time coverage is `average_coverage_seconds`; `score_coverage_seconds`
reports the same raw-input time coverage for compatibility, not score weighting.
Pressure completeness uses `pressure_sample_count` versus `average_seconds / 60`.
Unknown historical counts stay null. Omitted `average` defaults to 1800 seconds;
unsupported average/window values return 400 without caching.

All charts use `/api/scores` with both `window` and `average` in the query string.
The app caches up to 16 recent combinations for 30 seconds, with age-adjusted
shared-cache headers. Cloudflare must retain **the full query string** in its
cache key; the existing default-key rule needs no change. Retention is 38 days
so even the start of a 30d chart can have a full 7d averaging lookback once enough
data has accumulated. Existing records are preserved; no backfill is invented.
During a rollout, the new UI rejects a legacy cached payload instead of labeling
its double-averaged values as corrected; the normal refresh retries after expiry.

## Cloudflare analytics and privacy

The app has no visitor counters, IP attribution, session identifiers, analytics
POST endpoints, or access logs. Only public model history is collected locally.
Cloudflare's dashboard is the place for traffic statistics; those figures are
not published on the graph. Cloudflare metrics are not equivalent to the old
custom active-viewer or watch-time estimates.

The live hostname is <https://darkbloom.benbuschmann.com/>. It is proxied through
Cloudflare. Enable **Web Analytics → Add a site → this hostname → automatic
setup** in Cloudflare if not already enabled. Cloudflare then injects its own
beacon at the edge; this repo needs no analytics token or manually embedded
tracker. The HTML security policy permits Cloudflare's beacon host and
same-origin reports. No proxy subnet/IP configuration is needed.

See [Cloudflare setup](https://developers.cloudflare.com/web-analytics/get-started/#sites-proxied-through-cloudflare)
and [CSP requirements](https://developers.cloudflare.com/web-analytics/faq/#what-do-i-need-to-add-to-my-content-security-policy-csp).
Enabling this account setting is separate from pushing or deploying the repo.
Cloudflare and reverse-proxy logs have their own data handling and retention.

### Upgrading from custom visitor tracking

On startup, the new service drops only the legacy `traffic_events` table and
its indexes with SQLite secure deletion enabled. **Old visitor statistics are
removed; model scores and pressure samples are preserved.** This is idempotent.
It does not erase separate backups, filesystem snapshots, or proxy/CDN logs.
Past visitor statistics are recoverable only from an existing separate backup.

Remove obsolete `PUBLIC_ORIGIN` and `TRUSTED_PROXY_CIDRS` variables at your
convenience; the service ignores them. If a custom start command includes the
old `--public-origin` or `--trusted-proxy-cidrs` flags, remove those flags
before upgrading. The Dockerfile's default command needs no change.

Run this small standard-library HTTP service behind Traefik/Cloudflare with
reasonable request limits. It is intended for modest traffic, not an unlimited
API.

Independent community project. [MIT license](LICENSE).
# Optional public provider estimate pages

The directory also accepts the JSON copied from
`https://console.darkbloom.dev/api/me/providers` while logged in. It extracts
`providers[].se_public_key` in the browser and submits only the deduplicated
public keys to the existing fleet endpoint. The full JSON is never sent or
saved by this tool and is cleared after successful creation. Invalid JSON or
providers missing keys are rejected rather than silently creating a partial fleet.

The `/providers` directory includes **Create your fleet**: paste 1–26 complete
SE public keys, one per line, to save an unlisted random fleet URL. Duplicate keys
are combined; membership persists in SQLite across deploys and connections are
resolved through the public attestation feed. Unobserved keys wait for public
data. No account JSON, account identifiers, credentials or visitor IPs are stored.
Fleet membership is user-selected, not proof of ownership. Anyone with the link
can view it; it is not authenticated. Creation is bounded to 20 fleets per minute
globally and 10,000 saved fleets, with request-size and same-origin checks.

Set `TRACK_ALL_PUBLIC_PROVIDERS=1` to record every public provider from the same
shared minute stats pull. `/providers` is a paginated directory searchable by
SE public key, chip or model; `/providers/se-<key-fingerprint>` combines observed
connections of that key into hourly estimates. Keys are joined from the public
attestation endpoint once per minute. Connection IDs remain internal accounting
handles; existing ID links remain compatible but are not listed or searchable.
Departed connections keep their recorded history. Unknown keys return
404 and never trigger an upstream request or create database records.
Directory entries never disclose private aggregate-page slugs or account
membership. Your configured fleet pages continue working separately, unlisted.
Provider API responses use a 64-entry, 30-second server cache, invalidated on
collection or errors. No additional Cloudflare rule is necessary.

Set `PROVIDER_PAGES_JSON` to a JSON object mapping a page slug to an ordered
list of public provider IDs, or set `PROVIDER_PAGES_FILE` to a private JSON file
with the same structure. Keep real identity configuration outside Git.
The page is `/providers/<slug>`; only configured slugs are enabled.

For the owner's local configuration, run with
`PROVIDER_PAGES_FILE=data/provider-pages.json python3 server.py serve` and visit
`http://127.0.0.1:8788/providers/<configured-slug>`. Use a long random slug for
an unlisted page. There are no navigation links to or from the score charts,
and pages send no-index directives. This is not authentication: anyone who
knows the URL can view and share it.
For Dokploy, configure the JSON environment variable on the application and
redeploy with the existing persistent `/data` volume. Configuration is not
shipped inside the image. Do not upload account responses or credentials.

### v0.5.0 — public estimates and accounting guardrails

Provider collection uses `https://api.darkbloom.dev/v1/stats` directly and its
`snapshot_at`, never the console cache or local poll time for counter boundaries.
Attestation is fetched and mapped **before** stats each cycle, with one bounded
retry before capture when the stats snapshot contains unmapped connections.
Unmapped usage is still saved internally; later verified mappings can join it.
Duplicate or
out-of-order snapshots cannot move counter baselines. The directory still uses
the same no-store edge policy and bounded server-side cache.

`tokens_generated` and `requests_served` deltas are retained per connection/hour/
catalog. Requests are **observed counter increases, not exact paid jobs**;
cross-hour intervals are allocated proportionally. Negative resets and intervals
over 150 seconds are excluded. Positive token jumps above 20× a rolling positive
rate p95 (at least five accepted intervals) are rejected, with a bootstrap and
absolute ceiling of 10,000 tokens/second. Requests also have a 1,000/second cap.
A zero-baseline session restoring at least another session's last lifetime count
for the same public key is excluded. These conservative heuristics can reject
real bursts; they do not prove a reset. Rejected observations become baselines
without being counted as work. Only 32 positive rates per session are retained.

**Ratio source:** `GET /v1/network/series?window=24h`, validated as aligned,
closed 1,800-second buckets. An hour gets a ratio only with both buckets and a
positive output denominator. Sum input / sum output, never average bucket ratios.
Old `public_network_minutes` data is preserved but no longer ingested or read.
Historical complete series buckets can populate the last 24h; no provider usage
is invented. Current-hour ratio estimates deliberately remain unavailable.

**Three alternatives, not a confidence interval:**

- Output-only API-price proxy, excluding unknown input. Not a guaranteed payout floor.
- Rolling network payout proxy: `work_earnings_micro_usd / 1e6` from
  `/v1/network/totals?window=24h` divided by `last_24h_completion_tokens` from
  the direct stats feed. `totals.tokens` includes input and is NOT the denominator.
  Source timestamps must be within 120 seconds; stale calibration is not captured.
  Each hourly calibration is the latest observed rolling-24h rate during that hour,
  not an exact hourly earnings rate. Historical hours without a saved rate stay unknown.
- Hourly input-ratio API-price proxy. Per-machine input is publicly unobservable.

The chart picker defaults to the network payout proxy. All alternatives are in
the breakdown/tooltip. Missing estimates stay unknown, rather than zero. The
public `models` array is an **advertised catalog, not loaded models**. Stable
current-model intervals with a multi-model catalog conservatively get a “Shared
catalog” range over all advertised prices; any missing price makes the range
unavailable. Genuine current-model changes stay unattributed and unpriced by API
rates (the network payout proxy is still available). These are not per-model
measured token allocations. Public model demand has request counts only.

Coverage is aggregated per computer/hour: unique minute samples /60, longest
interval, >150s gap counts, resets/jumps, ambiguous-output share, ratio buckets /2,
and price freshness. A gap is recorded in every overlapping hour. Concurrent
sessions' minute/eligibility bitmaps are unioned by public key, never summed.
There are no raw per-poll or per-job records. Legacy request/coverage data stays
unknown; known request deltas appended to legacy hours are visible but explicitly
partial. Sparse sampling can undercount and hour-boundary allocation is approximate.
Rejected or missing payout calibration is surfaced as a warning with source
alignment/age, not silently displayed as zero revenue.

**Base scenario:** the official monthly memory tiers are 24/$10, 32/$12,
48/$16, 64/$18, 96/$22, 128/$26, 192/$30, 512/$40. UTC calendar-month proration
and `clamp((uptime-.9)/.1,0,1)` follow the source, applied separately to twelve
closed 5m blocks using eligible minute samples. Public eligibility requires
explicit App Attest authorization, online/serving statuses, reported OS ≥27 and
a current model. Missing minute samples fail closed. Results are “up to” scenarios
for ALL tiers, not confirmed rewards or guaranteed ceilings: private health,
ownership/binding, OS-bound authorization, pool slots, verified-memory limits,
reduction settings and true second-level session uptime are unavailable. They
are never mixed into job charts; partial-hour base stays pending.

Reviewed source (2026-10-08):
[tiers and availability](https://github.com/Layr-Labs/d-inference/blob/master/coordinator/payments/baserewards/floor.go),
[private eligibility](https://github.com/Layr-Labs/d-inference/blob/master/coordinator/payments/baserewards/machine_candidates.go),
[calendar proration](https://github.com/Layr-Labs/d-inference/blob/master/coordinator/internal/payments/rewardpolicy/epoch.go).

Migration is additive and makes a verified private online backup at
`/data/scores.sqlite3.before-provider-quality.backup` before upgrading an existing
provider schema. Preserve the same volume. Older releases use positional INSERTs
and are NOT safe to run against the expanded schema; rollback requires restoring
a consistent backup into a separate recovery volume, not overwriting the live DB.
Scores and configured/unlisted fleets are preserved. No account credentials are
added to this application. Leaderboard ownership inference is deliberately not
implemented; matching jobs alone is not proof of ownership.

The reported 18:14–18:22 UTC orphan was not verified: the private earnings endpoint
capped results at 1,000 and its history no longer covered that interval. No identities
were guessed or merged. An exported affected job list plus public mapping evidence
is required to complete that audit; this is separate from credential-free collection.
Computer labels use public chip names, numbered for duplicates; registration changes need explicit
configuration changes, not guesses about physical machine identity.
Hourly history is retained for 38 days, with no per-request or visitor records.
