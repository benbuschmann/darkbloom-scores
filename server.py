#!/usr/bin/env python3
"""Read-only public score feed for the Darkbloom manager's model comparison.

This service uses only public network capacity and pricing. It never reads a
provider configuration, API key, earnings record, or local manager state.
"""

from __future__ import annotations

import argparse
import csv
import ipaddress
import json
import os
import re
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
IP_RETENTION_SECONDS = 7 * 86400
SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{10,80}$")
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
                CREATE TABLE IF NOT EXISTS traffic_events (
                    id INTEGER PRIMARY KEY,
                    at INTEGER NOT NULL,
                    session_id TEXT NOT NULL,
                    ip TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK (kind IN ('view','heartbeat')),
                    watched_seconds INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS traffic_events_time ON traffic_events(at);
                CREATE INDEX IF NOT EXISTS traffic_events_session ON traffic_events(session_id,at);
                CREATE INDEX IF NOT EXISTS traffic_events_ip_time ON traffic_events(ip,at);
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
            db.execute("DELETE FROM traffic_events WHERE at<?", (now - IP_RETENTION_SECONDS,))

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
            db.execute("DELETE FROM traffic_events WHERE at<?", (at - IP_RETENTION_SECONDS,))
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

    def record_traffic(self, kind: str, session_id: str, ip: str, now: int) -> None:
        if kind not in {"view", "heartbeat"} or not SESSION_ID.fullmatch(session_id):
            raise ValueError("invalid traffic event")
        with self.connect() as db:
            recent_from_ip = db.execute(
                "SELECT COUNT(*) FROM traffic_events WHERE ip=? AND at>=?",
                (ip[:64], now - 60),
            ).fetchone()[0]
            if recent_from_ip >= 120:
                return
            previous = db.execute(
                "SELECT at,kind FROM traffic_events WHERE session_id=? ORDER BY at DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            if kind == "heartbeat" and previous and previous[1] == "heartbeat" and now - previous[0] < 30:
                return
            watched = min(60, now - previous[0]) if kind == "heartbeat" and previous and 0 <= now - previous[0] <= 120 else 0
            db.execute(
                "INSERT INTO traffic_events(at,session_id,ip,kind,watched_seconds) VALUES(?,?,?,?,?)",
                (now, session_id, ip[:64], kind, watched),
            )
            db.execute("DELETE FROM traffic_events WHERE at<?", (now - IP_RETENTION_SECONDS,))

    def traffic_summary(self, now: int) -> dict[str, int | float]:
        with self.connect() as db:
            views, unique_ips, watched = db.execute("""
                SELECT COUNT(*) FILTER (WHERE kind='view'),
                       COUNT(DISTINCT ip) FILTER (WHERE kind='view'),
                       COALESCE(SUM(watched_seconds),0)
                FROM traffic_events WHERE at >= ?
            """, (now - 86400,)).fetchone()
            active = db.execute(
                "SELECT COUNT(DISTINCT session_id) FROM traffic_events "
                "WHERE kind='heartbeat' AND at>=?",
                (now - 120,),
            ).fetchone()[0]
        return {
            "active_viewers": active,
            "page_views_24h": views,
            "unique_ips_24h": unique_ips,
            "viewer_hours_24h": round(watched / 3600, 2),
        }

    def traffic_report(self, now: int) -> list[dict[str, object]]:
        """Operator-only local report; no HTTP route exposes raw addresses."""
        with self.connect() as db:
            rows = db.execute("""
                SELECT ip,
                       COUNT(*) FILTER (WHERE kind='view') AS views,
                       COALESCE(SUM(watched_seconds),0) AS watched_seconds,
                       MAX(at) AS last_seen
                FROM traffic_events WHERE at >= ?
                GROUP BY ip ORDER BY views DESC, watched_seconds DESC LIMIT 100
            """, (now - 86400,)).fetchall()
        return [
            {"ip": ip, "views": views, "viewer_minutes": round(seconds / 60, 1), "last_seen": iso_utc(last_seen)}
            for ip, views, seconds, last_seen in rows
        ]


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
    def __init__(self, store: ScoreStore, trusted_proxy_cidrs: str = "", public_origin: str = "") -> None:
        self.store = store
        self.prices = PriceCache()
        self.last_error: str | None = None
        self.trusted_proxies = [ipaddress.ip_network(item.strip()) for item in trusted_proxy_cidrs.split(",") if item.strip()]
        self.public_origin = public_origin.rstrip("/")
        if self.public_origin:
            parsed = urlparse(self.public_origin)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path or parsed.query or parsed.fragment or parsed.username:
                raise ValueError("PUBLIC_ORIGIN must be an http(s) origin without a path or credentials")

    def trusts_proxy(self, address: str) -> bool:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return False
        return any(ip in network for network in self.trusted_proxies)

    def client_ip(self, peer: str, forwarded_for: str | None, real_ip: str | None) -> str:
        if not self.trusts_proxy(peer):
            return peer
        # Walk from the known proxy toward the client; never trust an arbitrary
        # leftmost address supplied by a visitor. Traefik must sanitize headers.
        forwarded = forwarded_for or real_ip or ""
        try:
            chain = [str(ipaddress.ip_address(item.strip())) for item in forwarded.split(",") if item.strip()]
        except ValueError:
            return peer
        for address in reversed(chain):
            if not self.trusts_proxy(address):
                return address
        return chain[0] if chain else peer

    def capture(self, now: int | None = None) -> int:
        now = int(time.time()) if now is None else now
        capacity = fetch_capacity(DEFAULT_BASE_URL)
        prices, fallback = self.prices.read(now)
        count = self.store.record(capacity, prices, fallback, now)
        self.last_error = self.prices.last_error
        return count

    def loop(self, interval: int, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.capture()
            except (RuntimeError, OSError, sqlite3.Error, ValueError) as error:
                self.last_error = str(error)
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
        if parsed.path == "/api/traffic":
            self.send_json(self.server.service.store.traffic_summary(int(time.time())))  # type: ignore[attr-defined]
            return
        if parsed.path != "/api/scores":
            self.send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
            return
        try:
            window = int(parse_qs(parsed.query).get("window", ["7200"])[0])
            payload = self.server.service.store.read(window, int(time.time()))  # type: ignore[attr-defined]
        except (ValueError, sqlite3.Error) as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        payload["last_error"] = self.server.service.last_error  # type: ignore[attr-defined]
        self.send_json(payload)

    def do_POST(self) -> None:
        kind = {"/api/view": "view", "/api/heartbeat": "heartbeat"}.get(urlparse(self.path).path)
        if kind is None:
            self.send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
            return
        try:
            if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
                raise ValueError("JSON content type required")
            origin = self.headers.get("Origin")
            service = self.server.service  # type: ignore[attr-defined]
            scheme = self.headers.get("X-Forwarded-Proto", "http") if service.trusts_proxy(self.client_address[0]) else "http"
            expected_origin = service.public_origin or f"{scheme}://{self.headers.get('Host')}"
            if origin and origin != expected_origin:
                raise ValueError("cross-origin traffic event rejected")
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > 256:
                raise ValueError("invalid request size")
            payload = json.loads(self.rfile.read(length))
            session_id = str(payload["session_id"])
            ip = service.client_ip(self.client_address[0], self.headers.get("X-Forwarded-For"), self.headers.get("X-Real-IP"))
            service.store.record_traffic(kind, session_id, ip, int(time.time()))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, sqlite3.Error) as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        self.send_json({"ok": True})

    def send_bytes(self, body: bytes, content_type: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if content_type.startswith("text/html"):
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; base-uri 'none'")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, payload: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode("utf-8")
        self.send_bytes(body, "application/json; charset=utf-8", status)

    def log_message(self, format: str, *args: object) -> None:
        # Avoid a second, unbounded copy of visitor IPs in container logs.
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path(os.environ.get("SCORES_DB", "data/scores.sqlite3")))
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve = subparsers.add_parser("serve", help="collect public scores and serve read-only history")
    serve.add_argument("--host", default=os.environ.get("BIND_HOST", "127.0.0.1"))
    serve.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8788")))
    serve.add_argument("--interval", type=int, default=int(os.environ.get("POLL_INTERVAL_SECONDS", "60")))
    serve.add_argument("--trusted-proxy-cidrs", default=os.environ.get("TRUSTED_PROXY_CIDRS", ""))
    serve.add_argument("--public-origin", default=os.environ.get("PUBLIC_ORIGIN", ""))
    import_csv = subparsers.add_parser("import-csv", help="backfill public score history from a CSV")
    import_csv.add_argument("csv_path", type=Path)
    import_load = subparsers.add_parser("import-load-csv", help="seed the 15-minute average from a public load CSV")
    import_load.add_argument("csv_path", type=Path)
    subparsers.add_parser("traffic-report", help="show the last 24 hours of IP-level traffic locally")
    args = parser.parse_args()
    store = ScoreStore(args.db)
    if args.command == "import-csv":
        print(f"Imported {store.import_csv(args.csv_path)} public score rows")
        return
    if args.command == "import-load-csv":
        print(f"Imported {store.import_load_csv(args.csv_path, int(time.time()))} recent pressure samples")
        return
    if args.command == "traffic-report":
        print(json.dumps(store.traffic_report(int(time.time())), indent=2))
        return
    service = ScoreService(store, args.trusted_proxy_cidrs, args.public_origin)
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
