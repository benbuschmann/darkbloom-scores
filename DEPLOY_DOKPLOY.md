# Handoff: deploy Darkbloom Scores with Dokploy

Goal: host these public model-score and load charts on the owner's Dokploy server.
This repository is source, **not evidence of a deployment**. Do not deploy
the private local tracker or copy any API keys/personal data.

## Application settings

Create one **Application** using a public Git source or the owner's GitHub
integration. No private-repository access is necessary.

| Setting | Value |
| --- | --- |
| Repository | `https://github.com/benbuschmann/darkbloom-scores.git` |
| Branch | `main` |
| Build type | Dockerfile |
| Build context | Repository root (`.`) |
| Dockerfile | `Dockerfile` at repository root |
| Container / domain target port | `8788` |
| Replicas | **1** |
| Named persistent volume | `darkbloom-scores-data` mounted at **`/data`** |
| Health check | Image includes HTTP `/healthz` |
| Restart behavior | Restart on failure; no scale-to-zero/sleep |

Attach the volume in the application's advanced storage settings **before the
first deployment**. Select the new remote server and keep this service pinned
to that node: an ordinary Docker volume is local to its node. If remote builds
require a registry, use the owner's configured registry; do not assume one
already exists.

Start with modest resources, e.g. a 256 MB memory limit, and monitor actual
usage. Keep the image's start command. No extra database, Nginx, Darkbloom
provider, or Darkbloom credential is needed.

Create a domain route for `/`, target port `8788`, through Dokploy/Traefik.
A generated domain is an option. Check DNS and certificate availability for
the chosen hostname; use HTTPS for the public site. Do **not** publish 8788
directly to the internet or change unrelated applications/routes.

The current public hostname is `darkbloom.benbuschmann.com`. Runtime defaults
are in the Dockerfile; no visitor-attribution environment variables are needed.
Runtime requires outbound HTTPS to Darkbloom's public feeds, and builds require
GitHub access for the checksum-pinned dependency.

## Public score caching

The app shares serialized responses per supported time window for up to 30
seconds and invalidates them immediately after recording a new sample or
reporting a collection failure. Cache entries are keyed by `(window, average)`
and bounded to the 16 most recently used combinations. Healthy score responses use `Cache-Control: public, max-age=0,
s-maxage=N`, with N reduced by their age in the application cache. Browsers
must fetch fresh data; shared caches can reuse it for the remaining lifetime.
Empty history, collection failures, HTTP errors, HTML, and health checks stay
`no-store`. No additional service or dependency is required.

For Cloudflare Free, create a Cache Rule matching only GET requests to
`darkbloom.benbuschmann.com/api/scores`:

```text
(http.host eq "darkbloom.benbuschmann.com" and http.request.method eq "GET" and http.request.uri.path eq "/api/scores")
```

- Cache eligibility: **Eligible for cache**.
- Edge TTL: **Use cache-control header if present, bypass cache if not**.
- Browser TTL: **Respect origin TTL** (or leave the override unset).
- Keep the default cache key, including the **full query string**. Do not ignore
  either `window` or `average`.
- Do not set a fixed Edge TTL override or status-code TTL. The app supplies
  the short lifetime; the Free-plan fixed override minimum is unsuitable here.

Verify repeated requests return `CF-Cache-Status: HIT`, that `Age` stays within
the remaining lifetime, and that new score timestamps appear after expiration.
Different Cloudflare locations maintain separate caches. A restart empties the
app cache and leaves the database volume intact.

### Adding per-model charts

The per-model load/request charts use the same cached `/api/scores` response
and all eight existing time windows. Keep the deployed Cloudflare cache rule
unchanged. The image includes the new `model-charts.js` asset; no new service,
environment variable, scheduled job, or proxy route is needed.

On startup, an additive SQLite migration adds nullable count/average columns.
Preserve the current `/data` volume: existing scores remain visible and new
load/request history starts with the first collection after deployment.
Do not backfill counts from scores. A full time-weighted average requires the
selected duration of covered history; partial coverage is labeled in the chart.
Collection remains once per minute, and the saved manager-score formula is unchanged.

### v0.2.0: configurable moving averages

The new picker defaults to 30m and supports 15m through 7d independently of the
visible time window. Scores are smoothed from the saved manager-score history;
counts are averaged from raw observations. No history is rewritten. The response
adds `average_seconds`, release `version`, raw `saved_score`, and score coverage.
The database adds a model/time index and retains 38 days (30d view + 7d lookback
with a boundary margin). Preserve the same volume; existing history may have
shorter coverage until new data accumulates. No new environment variable is needed.

## Analytics: Cloudflare only

In Cloudflare Web Analytics, add/select this hostname and enable automatic
setup for a proxied site. This is a Cloudflare account setting, not a Dokploy
setting or a GitHub workflow. The app permits the edge-injected beacon in its
Content Security Policy and does not embed a second tracker or require an
analytics token. Confirm a beacon loads and data appears in Cloudflare's
dashboard; pushing the repo alone does not enable Web Analytics.

No app-side IP/session collection remains. No trusted proxy CIDR list needs
updating after network restarts. Old `PUBLIC_ORIGIN` and
`TRUSTED_PROXY_CIDRS` environment variables can be removed; they are ignored.
Remove the corresponding legacy CLI flags if a custom run command uses them.

**Upgrade migration:** starting the new image removes the old visitor table
and its indexes, with SQLite secure deletion enabled, without deleting model
score history. Existing backups/snapshots and proxy/CDN logs are not touched;
review their retention separately. Lost visitor statistics are recoverable
only from a separate existing backup, not from the app.

## Deploying a pushed change

The GitHub workflow tests the code and Docker image; it does **not** deploy.
Dokploy must watch the repository's `main` branch through its configured GitHub
integration/auto-deploy option or receive a deployment webhook for pushes.
Check the integration and webhook delivery in the owner's Dokploy/GitHub
settings if a push does not create a deployment. Do not publish webhook
secrets or change unrelated services. A manual Deploy/Rebuild is the fallback.
Preserve the named `/data` volume and one replica.

## Acceptance checks

1. Confirm GitHub Actions passed for the deployed commit.
2. `/healthz` should return `ok: true` and `version: "0.2.0"`.
3. `/api/scores?window=7200&average=1800` must receive new timestamped scores.
   Check the default average is 1800 seconds; startup averages label partial coverage.
4. Check combined model lines, label toggles, and every time-window button.
   Per-model cards should appear below, ranked by score. Verify live-request
   and saved-score toggles, values above 100%, and that all charts follow the
   selected window. API rows should contain the new count fields (null for
   old history, populated for fresh samples).
   Select a 7d average while keeping a 2h chart, then change the chart to 4h:
   the average must stay 7d. Verify separate cache hits for different `average`
   values; no Cloudflare purge is required when the new UI adds this query key.
5. The old public traffic cards must be absent. GET `/api/traffic` and POST
   `/api/view` / `/api/heartbeat` must return 404, and the browser must no
   longer send these requests. Check Cloudflare Web Analytics separately.
6. `/.env`, `/data/scores.sqlite3`, `/api/earnings`, and `/api/providers`
   must return 404.
7. Note an existing score, redeploy using the **same volume**, and verify
   earlier rows remain. Do not claim persistence without this check.
8. Confirm collection continues without a browser or the owner's Mac.

Report the public URL, exact commit, verified volume mount, and any failed
checks. Do not call GitHub publication itself a live deployment.

## Optional public-history import

The repo contains no database or historical CSV. It can collect from scratch.
If the owner wants earlier public history, transfer only `model-scores.csv`
to a temporary private location and run inside the container as UID 10001:

```sh
python server.py import-csv /temporary/model-scores.csv
```

The optional `python server.py import-load-csv /temporary/model-load.csv`
seeds only the latest 15 minutes. Never transfer the private tracker DB,
job/earnings records, provider machine history, or `.env`. Do not commit
imports to GitHub. Old rows outside retention are pruned normally.

## Updates and backups

- Rebuild from a tested commit; preserve one replica and the same volume.
- Bind-mount directories must be writable by UID/GID `10001:10001`; do not
  make them world-writable as a workaround.
- Use SQLite's online backup API, or stop the service for a consistent
  volume backup. Copying a live `.sqlite3` file alone can miss WAL data.
- Keep backups private with bounded retention; older backups may still contain
  the retired visitor data.
- Scores and count history are retained 38 days, pressure samples one hour. SQLite reuses
  freed pages, so file size need not shrink immediately.
- `/healthz` checks HTTP liveness. Monitor `last_sample_at` and `last_error`
  in `/api/scores` for upstream collection failures.

Official references: [Dokploy applications](https://docs.dokploy.com/docs/core/applications),
[remote servers](https://docs.dokploy.com/docs/core/remote-servers), and
[Cloudflare automatic analytics](https://developers.cloudflare.com/web-analytics/get-started/#sites-proxied-through-cloudflare).
