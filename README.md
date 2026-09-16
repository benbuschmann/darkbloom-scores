# Darkbloom Scores

Current release: **v0.2.0**.

A small public website comparing all Darkbloom model scores over time. One
Python service collects public capacity and pricing, saves SQLite history, and
serves plain HTML/SVG charts. No API key, paid database, provider installation,
or running Mac is needed.

**Deploying with Dokploy? Start with [DEPLOY_DOKPLOY.md](DEPLOY_DOKPLOY.md).**

## Features and scope

- Combined score chart with abbreviated, clickable endpoint labels.
- Per-model charts ranked by latest averaged score: green loaded-model and blue
  request averages, a toggleable light-gray live request line, and a toggleable
  score-average strip. Each card shows its latest average utilization and score.
- 30m, 1h, **2h default**, 4h, 12h, 24h, 7d, and 30d chart windows.
- Independent moving-average picker: 15m, **30m default**, 1h, 2h, 4h, 12h,
  24h, or 7d. Applies to counts and score charts; live requests stay unsmoothed.
- One cached response shared by all charts, keyed by both controls.
- Minute collection; the underlying manager score formula stays unchanged.
- 38-day public score/count retention, including averaging lookback;
  visitor analytics handled separately by Cloudflare.
- Dockerfile, offline tests, GitHub Actions image and persistence checks.

This is only the public model-chart website. It does **not** include the private tracker,
personal token earnings, fleet machine identities, existing databases, or any
credentials. The repo starts without historical data. Collection begins when
deployed; earlier windows stay empty unless public score history is imported
separately.

Existing deployments keep their saved scores. The added load/request fields
start collecting after this update is deployed; old counts remain unknown,
not zero. A full average needs that much recorded history; partial coverage
is labeled separately for counts and scores.

## Scoring

```text
pressure = active_requests / max(1, warm_providers)
blended_price = 0.85 × input_price + 0.15 × output_price
score = average_pressure_over_last_15_minutes × blended_price × model_weight
```

The average uses timestamped minute samples strictly inside the last 900
seconds, at most 15 samples—not the last 15 observations regardless of age.
Startup/gaps use only available samples; missing history is not invented.
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

The server averages raw counts and saved manager scores over elapsed seconds,
before display downsampling. Each observation is held until the next sample;
intervals longer than 150 seconds are excluded as collection gaps. Counts and
scores have separate coverage. A first observation is provisional with zero
minutes observed. Unknown values remain gaps, not zeroes. Partial history is
labeled rather than being presented as a full average.

The displayed score is a moving average **of the saved 15-minute manager
scores**, not a change to the underlying manager formula above. The source
score remains saved unchanged and is also returned as `saved_score` in the API.
The combined chart, amber strip, card score, and sort order all use the selected
score average. The headline percentage is average requests divided by average
loaded models, not the mean of individual sample percentages.

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

API schema version 3 includes the selected `average_seconds`, app `version`,
and each row's raw `loaded`, `requests`, `available_to_load`, and `saved_score`,
alongside `average_loaded`, `average_requests`, and smoothed `score`. Coverage
is reported as `average_coverage_seconds` and `score_coverage_seconds`.
Unknown historical counts stay null. Omitted `average` defaults to 1800 seconds;
unsupported average/window values return 400 without caching.

All charts use `/api/scores` with both `window` and `average` in the query string.
The app caches up to 16 recent combinations for 30 seconds, with age-adjusted
shared-cache headers. Cloudflare must retain **the full query string** in its
cache key; the existing default-key rule needs no change. Retention is 38 days
so even the start of a 30d chart can have a full 7d averaging lookback once enough
data has accumulated. Existing records are preserved; no backfill is invented.

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
