#!/usr/bin/env python3
"""Read-only public score feed for the Darkbloom manager's model comparison.

This service uses only public network capacity and pricing. It never reads a
provider configuration, API key, earnings record, or local manager state.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import signal
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from warm_model_manager import (
    DEFAULT_BASE_URL,
    DEFAULT_PRICING_URL,
    DEFAULT_WEIGHTS,
    CapacitySample,
    ModelPrice,
    fetch_capacity,
    fetch_model_prices,
)


WINDOW_BUCKETS = {
    1800: 60,
    3600: 60,
    7200: 60,
    14400: 60,
    43200: 60,
    86400: 120,
    604800: 900,
    2592000: 3600,
}
SAMPLE_WINDOW_SECONDS = 15 * 60
SCORE_RETENTION_SECONDS = 31 * 86400
SCORE_CACHE_SECONDS = 30
WEB_PATH = Path(__file__).with_name("index.html")


def iso_utc(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_utc(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


class ScoreStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            # Retire only the old visitor collection, never score history.
            # secure_delete clears freed SQLite pages; external backups/logs
            # have their own retention and are not touched by this migration.
            db.execute("PRAGMA secure_delete=ON")
            db.execute("DROP TABLE IF EXISTS traffic_events")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS pressure_samples (
                    at INTEGER NOT NULL,
                    model_id TEXT NOT NULL,
                    pressure REAL NOT NULL,
                    PRIMARY KEY (at, model_id)
                );
                CREATE TABLE IF NOT EXISTS scores (
                    at INTEGER NOT NULL,
                    model_id TEXT NOT NULL,
                    score REAL,
                    average_pressure REAL,
                    blended_price_usd REAL,
                    model_weight REAL NOT NULL,
                    sample_count INTEGER NOT NULL,
                    PRIMARY KEY (at, model_id)
                );
                CREATE INDEX IF NOT EXISTS scores_time ON scores(at);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA busy_timeout=10000")
        try:
            with db:
                yield db
        finally:
            db.close()

    def prune(self, now: int) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM pressure_samples WHERE at<?", (now - 3600,))
            db.execute("DELETE FROM scores WHERE at<?", (now - SCORE_RETENTION_SECONDS,))

    def record(
        self,
        capacity: dict[str, CapacitySample],
        prices: dict[str, ModelPrice],
        fallback: ModelPrice,
        now: int,
    ) -> int:
        at = now // 60 * 60
        records = []
        with self.connect() as db:
            for model_id, sample in capacity.items():
                db.execute(
                    "INSERT OR IGNORE INTO pressure_samples(at,model_id,pressure) VALUES(?,?,?)",
                    (at, model_id, sample.pressure),
                )
                samples = db.execute(
                    "SELECT pressure FROM pressure_samples WHERE model_id=? AND at>? AND at<=? "
                    "ORDER BY at DESC LIMIT 15",
                    (model_id, at - SAMPLE_WINDOW_SECONDS, at),
                ).fetchall()
                average = sum(row[0] for row in samples) / len(samples) if samples else None
                price = prices.get(model_id, fallback)
                blended = price.blended_usd
                weight = DEFAULT_WEIGHTS.get(model_id, 1.0)
                score = average * blended * weight if average is not None and blended is not None else None
                records.append((at, model_id, score, average, blended, weight, len(samples)))
            db.executemany(
                "INSERT OR IGNORE INTO scores(at,model_id,score,average_pressure,blended_price_usd,model_weight,sample_count) "
                "VALUES(?,?,?,?,?,?,?)",
                records,
            )
            db.execute("DELETE FROM pressure_samples WHERE at<?", (at - 3600,))
            db.execute("DELETE FROM scores WHERE at<?", (at - SCORE_RETENTION_SECONDS,))
        return len(records)

    def import_csv(self, csv_path: Path) -> int:
        """Backfill only the public score fields from the local score history."""
        records = []
        with csv_path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                try:
                    at = parse_utc(row["observed_at"]) // 60 * 60
                    model_id = row["model_id"].strip()
                    if not model_id:
                        continue
                    score = float(row["score"]) if row.get("score") else None
                    average = float(row["average_pressure"]) if row.get("average_pressure") else None
                    blended = float(row["blended_price_usd"]) if row.get("blended_price_usd") else None
                    weight = float(row["model_weight"]) if row.get("model_weight") else DEFAULT_WEIGHTS.get(model_id, 1.0)
                    count = int(row.get("sample_count") or 0)
                except (KeyError, ValueError, OverflowError):
                    continue
                records.append((at, model_id, score, average, blended, weight, count))
        with self.connect() as db:
            db.executemany(
                "INSERT OR IGNORE INTO scores(at,model_id,score,average_pressure,blended_price_usd,model_weight,sample_count) "
                "VALUES(?,?,?,?,?,?,?)",
                records,
            )
        return len(records)

    def import_load_csv(self, csv_path: Path, now: int) -> int:
        """Seed the rolling pressure window from public model-load snapshots."""
        latest_by_minute: dict[tuple[int, str], float] = {}
        cutoff = now - SAMPLE_WINDOW_SECONDS
        with csv_path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                try:
                    observed = parse_utc(row["observed_at"])
                    if observed < cutoff or observed > now + 60:
                        continue
                    model_id = row["model_id"].strip()
                    loaded = max(0, int(row["loaded"]))
                    requests = max(0, int(row["in_progress"]))
                    if model_id:
                        latest_by_minute[(observed // 60 * 60, model_id)] = requests / max(1, loaded)
                except (KeyError, ValueError, OverflowError):
                    continue
        with self.connect() as db:
            db.executemany(
                "INSERT OR IGNORE INTO pressure_samples(at,model_id,pressure) VALUES(?,?,?)",
                ((at, model_id, pressure) for (at, model_id), pressure in latest_by_minute.items()),
            )
        return len(latest_by_minute)

    def read(self, window: int, now: int) -> dict[str, object]:
        if window not in WINDOW_BUCKETS:
            raise ValueError("unsupported time window")
        bucket = WINDOW_BUCKETS[window]
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            latest = db.execute("SELECT MAX(at) FROM scores").fetchone()[0]
            rows = db.execute("""
                WITH last_in_bucket AS (
                    SELECT model_id, at / ? AS bucket, MAX(at) AS at
                    FROM scores WHERE at >= ?
                    GROUP BY model_id, bucket
                )
                SELECT scores.at, scores.model_id, scores.score
                FROM scores JOIN last_in_bucket
                  ON scores.model_id = last_in_bucket.model_id AND scores.at = last_in_bucket.at
                ORDER BY scores.at, scores.model_id
            """, (bucket, now - window)).fetchall()
        return {
            "generated_at": iso_utc(now),
            "last_sample_at": iso_utc(latest) if latest is not None else None,
            "window_seconds": window,
            "bucket_seconds": bucket,
            "rows": [
                {"observed_at": iso_utc(row["at"]), "model_id": row["model_id"], "score": row["score"]}
                for row in rows
            ],
        }
class PriceCache:
    def __init__(self) -> None:
        self.prices: dict[str, ModelPrice] = {}
        self.fallback = ModelPrice(None, None)
        self.next_refresh = 0
        self.last_error: str | None = None

    def read(self, now: int) -> tuple[dict[str, ModelPrice], ModelPrice]:
        if now >= self.next_refresh:
            try:
                self.prices, self.fallback = fetch_model_prices(DEFAULT_PRICING_URL)
                self.next_refresh = now + 15 * 60
                self.last_error = None
            except (RuntimeError, OSError, ValueError) as error:
                self.next_refresh = now + 60
                self.last_error = str(error)
        return self.prices, self.fallback


class ScoreService:
    def __init__(self, store: ScoreStore) -> None:
        self.store = store
        self.prices = PriceCache()
        self.last_error: str | None = None
        # Eight supported windows bound memory use; the lock also prevents a
        # burst of visitors from computing the same uncached window repeatedly.
        self._scores_lock = threading.Lock()
        self._scores_cache: dict[int, tuple[float, bytes, bool]] = {}

    def read_scores(self, window: int) -> tuple[bytes, int]:
        if window not in WINDOW_BUCKETS:
            raise ValueError("unsupported time window")
        with self._scores_lock:
            cached = self._scores_cache.get(window)
            if cached is None or time.monotonic() >= cached[0]:
                payload = self.store.read(window, int(time.time()))
                payload["last_error"] = self.last_error
                body = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode("utf-8")
                healthy = self.last_error is None and payload["last_sample_at"] is not None
                cached = (time.monotonic() + SCORE_CACHE_SECONDS, body, healthy)
                self._scores_cache[window] = cached
            expires, body, healthy = cached
            # Deduct time spent in the app cache so CDN caching does not add
            # another complete freshness period on top of an old response.
            ttl = max(0, int(expires - time.monotonic())) if healthy else 0
            return body, ttl

    def capture(self, now: int | None = None) -> int:
        now = int(time.time()) if now is None else now
        capacity = fetch_capacity(DEFAULT_BASE_URL)
        prices, fallback = self.prices.read(now)
        with self._scores_lock:
            count = self.store.record(capacity, prices, fallback, now)
            self.last_error = self.prices.last_error
            self._scores_cache.clear()
        return count

    def loop(self, interval: int, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.capture()
            except (RuntimeError, OSError, sqlite3.Error, ValueError) as error:
                with self._scores_lock:
                    self.last_error = str(error)
                    self._scores_cache.clear()
                print(f"Score capture failed: {error}", flush=True)
            try:
                self.store.prune(int(time.time()))
            except sqlite3.Error as error:
                print(f"Score retention cleanup failed: {error}", flush=True)
            stop.wait(interval)


class ScoreHandler(BaseHTTPRequestHandler):
    server: ThreadingHTTPServer

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(10)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self.send_bytes(WEB_PATH.read_bytes(), "text/html; charset=utf-8")
            return
        if parsed.path == "/healthz":
            self.send_json({"ok": True})
            return
        if parsed.path != "/api/scores":
            self.send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
            return
        try:
            window = int(parse_qs(parsed.query).get("window", ["7200"])[0])
            body, ttl = self.server.service.read_scores(window)  # type: ignore[attr-defined]
        except (ValueError, sqlite3.Error) as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        cache_control = f"public, max-age=0, s-maxage={ttl}" if ttl else "no-store"
        self.send_bytes(body, "application/json; charset=utf-8", cache_control=cache_control)

    def do_POST(self) -> None:
        # Old browser tabs may still send heartbeats after an upgrade.
        # Reject without reading, attributing, logging, or storing the body.
        self.close_connection = True
        self.send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)

    def send_bytes(self, body: bytes, content_type: str, status: HTTPStatus = HTTPStatus.OK,
                   *, cache_control: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if content_type.startswith("text/html"):
            # Allow Cloudflare's beacon host, including versioned script paths.
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline' https://static.cloudflareinsights.com; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; base-uri 'none'")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, payload: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode("utf-8")
        self.send_bytes(body, "application/json; charset=utf-8", status)

    def log_message(self, format: str, *args: object) -> None:
        # Do not collect visitor addresses or URLs in application access logs.
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path(os.environ.get("SCORES_DB", "data/scores.sqlite3")))
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve = subparsers.add_parser("serve", help="collect public scores and serve read-only history")
    serve.add_argument("--host", default=os.environ.get("BIND_HOST", "127.0.0.1"))
    serve.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8788")))
    serve.add_argument("--interval", type=int, default=int(os.environ.get("POLL_INTERVAL_SECONDS", "60")))
    import_csv = subparsers.add_parser("import-csv", help="backfill public score history from a CSV")
    import_csv.add_argument("csv_path", type=Path)
    import_load = subparsers.add_parser("import-load-csv", help="seed the 15-minute average from a public load CSV")
    import_load.add_argument("csv_path", type=Path)
    args = parser.parse_args()
    store = ScoreStore(args.db)
    if args.command == "import-csv":
        print(f"Imported {store.import_csv(args.csv_path)} public score rows")
        return
    if args.command == "import-load-csv":
        print(f"Imported {store.import_load_csv(args.csv_path, int(time.time()))} recent pressure samples")
        return
    service = ScoreService(store)
    server = ThreadingHTTPServer((args.host, args.port), ScoreHandler)
    server.service = service  # type: ignore[attr-defined]
    stop = threading.Event()
    collector = threading.Thread(target=service.loop, args=(max(60, args.interval), stop), daemon=True)
    collector.start()
    print(f"Public score feed listening on {args.host}:{args.port}", flush=True)
    def terminate(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminate)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
        collector.join(timeout=5)


if __name__ == "__main__":
    main()
