# Project instructions

This repository is the public Darkbloom all-model score website, not a provider
manager and not the owner's private local tracker.

- Keep the public service small: Python standard library, SQLite, plain HTML/SVG.
- Use only public network capacity and pricing; no provider CLI or account keys.
- Visitor analytics belong in Cloudflare. Do not reintroduce app-side IP/session
  storage, heartbeat endpoints, public traffic counters, or proxy attribution.
- Never publish `.env`, SQLite databases, CSV history, visitor IP records,
  personal earnings, machine/provider identities, or deployment credentials.
- Do not modify the downloaded manager module. Its upstream commit and hash
  are pinned in `fetch_manager.py`; review upstream changes before updating.
- Run `python3 fetch_manager.py`, `python3 -m unittest -v`,
  `node --test test_frontend.js`, and syntax checks
  before publishing changes. GitHub Actions also checks the Docker image.
- Per-model charts share the existing cached `/api/scores` response. Preserve
  Cloudflare cache headers and keys; do not invent historical counts from scores.
- Dokploy handoff is in `DEPLOY_DOKPLOY.md`: one replica, persistent `/data`,
  container port 8788, HTTPS, Cloudflare analytics, score-retention checks.
- Keep repo publication separate from deployment. Do not claim the site is
  hosted until its public URL, live collection, and persistence are verified.
- Preserve real data volumes during redeployment. Use consistent SQLite
  backups and never expose them through the public HTTP server.
