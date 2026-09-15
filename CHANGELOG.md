# Changelog

## Unreleased

- Remove public traffic cards and all custom visitor-IP/session collection,
  heartbeat writes, traffic API/reporting, and proxy-attribution settings.
- Retire the legacy visitor table on startup while preserving score history.
- Permit Cloudflare's automatically injected Web Analytics beacon and document
  its separate dashboard setup. No analytics credential is needed by the app.
- Keep model collection, scoring, chart windows, and minute refresh unchanged.
