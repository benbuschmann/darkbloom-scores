#!/usr/bin/env python3
"""Read-only public score feed for the Darkbloom manager's model comparison.

This service uses only public network capacity and pricing. It never reads a
provider configuration, API key, earnings record, or local manager state.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import signal
import sqlite3
import threading
import time
from collections import OrderedDict, deque
from contextlib import contextmanager
from dataclasses import dataclass
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
    get_json,
    pressure_samples,
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
AVERAGE_WINDOWS = (900, 1800, 3600, 7200, 14400, 43200, 86400, 604800)
DEFAULT_AVERAGE_SECONDS = 1800
# 30-day display + 7-day averaging lookback + one day for boundary samples.
SCORE_RETENTION_SECONDS = 38 * 86400
SCORE_CACHE_SECONDS = 30
SCORE_CACHE_ENTRIES = 16
WEB_PATH = Path(__file__).with_name("index.html")
APP_VERSION = WEB_PATH.with_name("VERSION").read_text(encoding="utf-8").strip()
COUNT_GAP_SECONDS = 150


@dataclass(frozen=True)
class PublicCapacitySample(CapacitySample):
    available_to_load: int | None = None


def fetch_capacity(base_url: str) -> dict[str, PublicCapacitySample]:
    """One public request supplies both the upstream score inputs and counts."""
    payload = get_json(f"{base_url.rstrip('/')}/api/models/capacity")
    rows = [row for row in payload.get("models", []) if isinstance(row, dict)]
    samples = pressure_samples(rows)
    availability = {}
    for row in rows:
        model_id = str(row.get("id") or row.get("model_id") or "")
        value = row.get("cold_providers")
        try:
            availability[model_id] = max(0, int(value)) if value is not None else None
        except (ValueError, TypeError, OverflowError):
            availability[model_id] = None
    return {
        key: PublicCapacitySample(key, sample.warm_providers, sample.active_requests,
                                  sample.pressure, availability.get(key))
        for key, sample in samples.items()
    }


def count_averages(samples: list[tuple], now: int) -> tuple[float | None, float | None, int]:
    """Time-weighted counts over 900 elapsed seconds, excluding collection gaps."""
    loaded_area = requests_area = 0.0
    covered = 0
    for previous, current in zip(samples, samples[1:]):
        if previous[1] is None or previous[2] is None:
            continue
        if not 0 < current[0] - previous[0] <= COUNT_GAP_SECONDS:
            continue
        elapsed = min(now, current[0]) - max(now - SAMPLE_WINDOW_SECONDS, previous[0])
        if elapsed > 0:
            loaded_area += previous[1] * elapsed
            requests_area += previous[2] * elapsed
            covered += elapsed
    if covered:
        return loaded_area / covered, requests_area / covered, covered
    # The initial observation has no measured duration yet. Do not fabricate
    # earlier counts from old scores or extend a stale count across an outage.
    latest = samples[-1] if samples else (now, None, None)
    return latest[1], latest[2], 0


def iso_utc(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_utc(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


def moving_average_rows(rows, average_seconds: int):
    """Average raw pressure once, then price it at the plotted observation.

    Match the manager's arithmetic mean of samples in (t - window, t],
    including the current sample. Count lines keep elapsed-time weighting.
    Legacy saved scores are returned for audit only, never as calculation input.
    """
    segments = deque()
    pressures = deque()
    pressure_total = 0.0
    totals = [0.0, 0.0]  # loaded, requests × seconds
    coverage = 0
    max_samples = average_seconds // 60
    previous = None

    def valid(value):
        return value is not None and math.isfinite(value) and value >= 0

    def pressure(row):
        if valid(row["loaded"]) and valid(row["requests"]):
            return row["requests"] / max(1, row["loaded"])
        return None

    def account(segment, duration):
        nonlocal coverage
        _, _, loaded, requests = segment
        if valid(loaded) and valid(requests):
            totals[0] += loaded * duration
            totals[1] += requests * duration
            coverage += duration

    for source in rows:
        row = dict(source)
        at = row["at"]
        if previous is not None and 0 < at - previous["at"] <= COUNT_GAP_SECONDS:
            segment = [previous["at"], at, previous["loaded"], previous["requests"]]
            segments.append(segment)
            account(segment, at - previous["at"])
        cutoff = at - average_seconds
        while segments and segments[0][1] <= cutoff:
            segment = segments.popleft()
            account(segment, segment[0] - segment[1])
        if segments and segments[0][0] < cutoff:
            account(segments[0], segments[0][0] - cutoff)
            segments[0][0] = cutoff
        if not coverage:
            totals[0] = totals[1] = 0.0
        while pressures and pressures[0][0] <= cutoff:
            pressure_total -= pressures.popleft()[1]
        if not pressures:
            pressure_total = 0.0
        current_pressure = pressure(row)
        if current_pressure is not None:
            pressures.append((at, current_pressure))
            pressure_total += current_pressure
        while len(pressures) > max_samples:
            pressure_total -= pressures.popleft()[1]
        average_pressure = max(0.0, pressure_total / len(pressures)) if pressures and current_pressure is not None else None
        price, weight = row.get("blended_price_usd"), row.get("model_weight")
        # A first observation is provisional; its coverage is explicitly zero.
        row["average_loaded"] = max(0.0, totals[0] / coverage) if coverage and valid(row["loaded"]) else row["loaded"]
        row["average_requests"] = max(0.0, totals[1] / coverage) if coverage and valid(row["requests"]) else row["requests"]
        row["saved_score"] = row["score"]
        row["pressure"] = current_pressure
        row["average_pressure"] = average_pressure
        row["pressure_sample_count"] = len(pressures)
        row["score"] = average_pressure * price * weight if average_pressure is not None and valid(price) and price > 0 and valid(weight) else None
        row["average_coverage_seconds"] = coverage
        row["score_coverage_seconds"] = coverage  # Raw-input time coverage; not a score weighting.
        previous = source
        yield row


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
                CREATE INDEX IF NOT EXISTS scores_model_time ON scores(model_id, at);
            """)
            # Additive migration: existing score rows stay valid; unavailable
            # historical counts remain NULL, not invented zeroes.
            for table, additions in {
                "pressure_samples": {"loaded": "INTEGER", "requests": "INTEGER"},
                "scores": {
                    "loaded": "INTEGER", "requests": "INTEGER", "available_to_load": "INTEGER",
                    "average_loaded": "REAL", "average_requests": "REAL",
                    "average_coverage_seconds": "INTEGER",
                },
            }.items():
                columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
                for name, column_type in additions.items():
                    if name not in columns:
                        db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {column_type}")

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
                    "INSERT OR IGNORE INTO pressure_samples(at,model_id,pressure,loaded,requests) VALUES(?,?,?,?,?)",
                    (at, model_id, sample.pressure, sample.warm_providers, sample.active_requests),
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
                counts = db.execute(
                    "SELECT at,loaded,requests FROM pressure_samples WHERE model_id=? AND at>=? AND at<=? ORDER BY at",
                    (model_id, at - SAMPLE_WINDOW_SECONDS - COUNT_GAP_SECONDS, at),
                ).fetchall()
                average_loaded, average_requests, coverage = count_averages(counts, at)
                records.append((at, model_id, score, average, blended, weight, len(samples),
                                sample.warm_providers, sample.active_requests,
                                getattr(sample, "available_to_load", None),
                                average_loaded, average_requests, coverage))
            db.executemany(
                "INSERT OR IGNORE INTO scores(at,model_id,score,average_pressure,blended_price_usd,model_weight,sample_count,"
                "loaded,requests,available_to_load,average_loaded,average_requests,average_coverage_seconds) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
        latest_by_minute: dict[tuple[int, str], tuple[float, int, int]] = {}
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
                        latest_by_minute[(observed // 60 * 60, model_id)] = (requests / max(1, loaded), loaded, requests)
                except (KeyError, ValueError, OverflowError):
                    continue
        with self.connect() as db:
            db.executemany(
                "INSERT OR IGNORE INTO pressure_samples(at,model_id,pressure,loaded,requests) VALUES(?,?,?,?,?)",
                ((at, model_id, *counts) for (at, model_id), counts in latest_by_minute.items()),
            )
        return len(latest_by_minute)

    def read(self, window: int, now: int, average: int = DEFAULT_AVERAGE_SECONDS) -> dict[str, object]:
        if window not in WINDOW_BUCKETS:
            raise ValueError("unsupported time window")
        if average not in AVERAGE_WINDOWS:
            raise ValueError("unsupported moving average")
        bucket = WINDOW_BUCKETS[window]
        start = now - window
        lookback = start - average - COUNT_GAP_SECONDS
        result = []
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            latest = db.execute("SELECT MAX(at) FROM scores").fetchone()[0]
            models = db.execute("SELECT DISTINCT model_id FROM scores WHERE at>=? AND at<=?", (start, now)).fetchall()
            for model in models:
                rows = db.execute("""
                    SELECT at, model_id, score, loaded, requests, available_to_load,
                           blended_price_usd, model_weight
                    FROM scores WHERE model_id=? AND at>=? AND at<=? ORDER BY at
                """, (model[0], lookback, now))
                buckets = {}
                for row in moving_average_rows(rows, average):
                    if row["at"] >= start:
                        buckets[row["at"] // bucket] = row
                result.extend(buckets.values())
        result.sort(key=lambda row: (row["at"], row["model_id"]))
        return {
            "schema_version": 4,
            "score_method": "mean_pressure_times_price_weight",
            "version": APP_VERSION,
            "generated_at": iso_utc(now),
            "last_sample_at": iso_utc(latest) if latest is not None else None,
            "window_seconds": window,
            "average_seconds": average,
            "bucket_seconds": bucket,
            "rows": [
                {"observed_at": iso_utc(row["at"]), **{key: row[key] for key in row.keys() if key != "at"}}
                for row in result
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
        # Independently keyed windows/averages, with bounded LRU memory use.
        # The lock coalesces a burst of visitors requesting the same response.
        self._scores_lock = threading.Lock()
        self._scores_cache: OrderedDict[tuple[int, int], tuple[float, bytes, bool]] = OrderedDict()

    def read_scores(self, window: int, average: int = DEFAULT_AVERAGE_SECONDS) -> tuple[bytes, int]:
        if window not in WINDOW_BUCKETS:
            raise ValueError("unsupported time window")
        if average not in AVERAGE_WINDOWS:
            raise ValueError("unsupported moving average")
        key = (window, average)
        with self._scores_lock:
            cached = self._scores_cache.get(key)
            if cached is None or time.monotonic() >= cached[0]:
                payload = self.store.read(window, int(time.time()), average)
                payload["last_error"] = self.last_error
                body = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode("utf-8")
                healthy = self.last_error is None and payload["last_sample_at"] is not None
                cached = (time.monotonic() + SCORE_CACHE_SECONDS, body, healthy)
                self._scores_cache[key] = cached
            self._scores_cache.move_to_end(key)
            while len(self._scores_cache) > SCORE_CACHE_ENTRIES:
                self._scores_cache.popitem(last=False)
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
        if parsed.path == "/model-charts.js":
            self.send_bytes(WEB_PATH.with_name("model-charts.js").read_bytes(), "text/javascript; charset=utf-8")
            return
        if parsed.path == "/healthz":
            self.send_json({"ok": True, "version": APP_VERSION})
            return
        if parsed.path != "/api/scores":
            self.send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
            return
        try:
            query = parse_qs(parsed.query)
            window = int(query.get("window", ["7200"])[0])
            average = int(query.get("average", [str(DEFAULT_AVERAGE_SECONDS)])[0])
            body, ttl = self.server.service.read_scores(window, average)  # type: ignore[attr-defined]
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
