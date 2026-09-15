# Handoff: deploy Darkbloom Scores with Dokploy

Goal: host this public score-only site on the owner's new Dokploy server.
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

Set in Dokploy's environment:

```text
PUBLIC_ORIGIN=https://your-chosen-hostname
```

Other runtime defaults are in the Dockerfile. Configure the origin/domain,
then deploy. Runtime needs outbound HTTPS to Darkbloom's public capacity and
pricing endpoints; builds also need GitHub access for the pinned dependency.

## Correct visitor IPs behind Traefik

Forwarded IP headers are ignored by default. Until proxy trust is configured,
the app sees proxy IPs and unique-IP numbers will be inaccurate.

1. Inspect the actual network and peer address used by the trusted Traefik
   proxy when connecting to this container.
2. Set `TRUSTED_PROXY_CIDRS` to that specific proxy address/subnet. **Do not
   guess a subnet or trust `0.0.0.0/0`, `::/0`, or every private network.**
3. Traefik must sanitize forwarded headers from internet clients; do not
   enable its insecure forwarded-header trust mode. If a CDN is in front,
   configure only its verified proxy ranges at Traefik.
4. Confirm `X-Forwarded-For` and `X-Forwarded-Proto`, then test two known
   clients. A visitor-supplied spoofed header must not change the stored IP.

Keep raw addresses/reports private. Application IP records are pruned after
seven days; proxy/CDN logs and backups have separate retention. Review those
settings and the footer disclosure before launch.

## Acceptance checks

1. Confirm GitHub Actions passed for the deployed commit.
2. `/healthz` should return `{"ok":true}`.
3. `/api/scores?window=7200` must receive new timestamped scores. Initial
   scores use partial history; full rolling averages build over 15 minutes.
4. Check model lines, label toggles, and every time-window button.
5. `/api/traffic` must return aggregates only; heartbeat requests should
   succeed over the public HTTPS origin. Never expose raw IPs via HTTP.
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
- Keep backups private with bounded retention; they may contain visitor IPs.
- Scores are retained 31 days, pressure samples one hour. SQLite reuses
  freed pages, so file size need not shrink immediately.
- `/healthz` checks HTTP liveness. Monitor `last_sample_at` and `last_error`
  in `/api/scores` for upstream collection failures.

Official references: [Dokploy applications](https://docs.dokploy.com/docs/core/applications)
and [remote servers](https://docs.dokploy.com/docs/core/remote-servers).
