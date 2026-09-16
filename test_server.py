"""Offline checks for public scores, privacy migration, and HTTP boundaries."""

import json
import sqlite3
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import server as score_feed
import fetch_manager
from warm_model_manager import CapacitySample, ModelPrice


class ScoreFeedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = score_feed.ScoreStore(Path(self.directory.name) / "scores.sqlite3")

    def test_public_capacity_adapter_uses_one_request_and_retains_availability(self) -> None:
        payload = {"models": [{"id": "model", "warm_providers": 10, "active_requests": 17, "cold_providers": 4}, None]}
        with patch.object(score_feed, "get_json", return_value=payload) as fetch:
            samples = score_feed.fetch_capacity("https://public.example/")
        fetch.assert_called_once_with("https://public.example/api/models/capacity")
        self.assertEqual(samples["model"].pressure, 1.7)
        self.assertEqual(samples["model"].available_to_load, 4)

    def seed_history(self, now, seconds):
        with self.store.connect() as db:
            db.executemany(
                "INSERT INTO scores(at,model_id,score,model_weight,sample_count,loaded,requests) VALUES(?,?,?,?,?,?,?)",
                ((at, "averages", 0.1 if at < now - 900 else 0.3, 1, 15,
                  10 if at < now - 900 else 30, 20 if at < now - 900 else 60)
                 for at in range(now - seconds, now + 1, 60)),
            )

    def test_default_is_30_elapsed_minutes_and_score_source_is_preserved(self):
        now = 1_700_000_040
        self.seed_history(now, 7200)
        default = self.store.read(7200, now)
        self.assertEqual(default["average_seconds"], 1800)
        self.assertEqual(default["version"], score_feed.APP_VERSION)
        latest = default["rows"][-1]
        self.assertEqual(latest["average_loaded"], 20)
        self.assertEqual(latest["average_requests"], 40)
        self.assertAlmostEqual(latest["score"], 0.2)
        self.assertEqual(latest["saved_score"], 0.3)
        self.assertEqual(latest["average_coverage_seconds"], 1800)
        fifteen = self.store.read(7200, now, 900)["rows"][-1]
        self.assertEqual(fifteen["average_loaded"], 30)
        self.assertAlmostEqual(fifteen["score"], 0.3)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT score FROM scores WHERE at=?", (now,)).fetchone()[0], 0.3)

    def test_week_average_reads_before_short_chart_and_survives_downsampling(self):
        now = 1_700_000_040
        self.seed_history(now, 604800 + 7200 + 60)
        short = self.store.read(7200, now, 604800)
        self.assertEqual(len(short["rows"]), 121)
        self.assertEqual(short["rows"][0]["average_coverage_seconds"], 604800)
        latest = short["rows"][-1]
        self.assertEqual(latest["average_coverage_seconds"], 604800)
        self.assertEqual(latest["score_coverage_seconds"], 604800)
        self.assertAlmostEqual(latest["average_loaded"], (10 * (604800 - 900) + 30 * 900) / 604800)
        self.assertAlmostEqual(latest["score"], (0.1 * (604800 - 900) + 0.3 * 900) / 604800)
        for window in score_feed.WINDOW_BUCKETS:
            last = self.store.read(window, now, 604800)["rows"][-1]
            for field in ("score", "average_loaded", "average_requests", "average_coverage_seconds"):
                self.assertEqual(last[field], latest[field])

    def test_partial_coverage_and_missing_values_are_not_filled(self):
        rows = [
            {"at":0,"loaded":10,"requests":20,"score":1},
            {"at":60,"loaded":None,"requests":None,"score":None},
            {"at":120,"loaded":30,"requests":60,"score":3},
            {"at":180,"loaded":30,"requests":60,"score":3},
            {"at":3600,"loaded":50,"requests":100,"score":5},
        ]
        result = list(score_feed.moving_average_rows(rows, 1800))
        self.assertEqual(result[0]["average_coverage_seconds"], 0)
        self.assertIsNone(result[1]["score"])
        self.assertIsNone(result[1]["average_loaded"])
        self.assertEqual(result[3]["average_coverage_seconds"], 120)
        self.assertEqual(result[3]["average_loaded"], 20)
        self.assertEqual(result[3]["score"], 2)
        self.assertEqual(result[4]["average_coverage_seconds"], 0)
        self.assertEqual(result[4]["score"], 5)

    def test_moving_average_clips_boundary_intervals_by_time(self):
        rows = [{"at":at,"loaded":at,"requests":at*2,"score":at/100} for at in range(0, 1981, 120)]
        actual = list(score_feed.moving_average_rows(rows, 900))[-1]
        now = rows[-1]["at"]
        expected = sum(max(0, min(now, b["at"]) - max(now-900, a["at"])) * a["loaded"] for a,b in zip(rows,rows[1:])) / 900
        self.assertEqual(actual["average_coverage_seconds"], 900)
        self.assertEqual(actual["average_loaded"], expected)
        self.assertAlmostEqual(actual["score"], expected/100)

    def test_average_cache_keys_are_independent_validated_and_lru_bounded(self):
        now = 1_700_000_040
        self.seed_history(now, 7200)
        service = score_feed.ScoreService(self.store)
        with patch.object(score_feed.time, "time", return_value=now), patch.object(score_feed.time, "monotonic", return_value=100):
            thirty, ttl = service.read_scores(7200)
            self.assertEqual(ttl, 30)
            fifteen, _ = service.read_scores(7200, 900)
            self.assertNotEqual(thirty, fifteen)
            self.assertIs(service.read_scores(7200, 1800)[0], thirty)
            for window in score_feed.WINDOW_BUCKETS:
                for average in score_feed.AVERAGE_WINDOWS:
                    body, ttl = service.read_scores(window, average)
                    data = json.loads(body)
                    self.assertEqual((data["window_seconds"],data["average_seconds"]), (window,average))
                    self.assertEqual(ttl, 30)
            self.assertEqual(len(service._scores_cache), score_feed.SCORE_CACHE_ENTRIES)
            with self.assertRaises(ValueError):
                service.read_scores(7200, 999)
            with self.assertRaises(ValueError):
                self.store.read(7200, now, 604801)

    def test_retention_keeps_week_lookback_for_30_day_chart(self):
        now = 1_700_000_040
        with self.store.connect() as db:
            db.executemany("INSERT INTO scores(at,model_id,score,model_weight,sample_count) VALUES(?, 'retained', 1, 1, 1)",
                           [(now-37*86400,), (now-39*86400,)])
        self.store.prune(now)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT at FROM scores").fetchall(), [(now-37*86400,)])

    def test_count_averages_are_elapsed_time_weighted_and_exclude_gaps(self) -> None:
        loaded, requests, seconds = score_feed.count_averages([(0, 10, 0), (60, 20, 20), (180, 30, 90)], 180)
        self.assertAlmostEqual(loaded, 50 / 3)
        self.assertAlmostEqual(requests, 40 / 3)
        self.assertEqual(seconds, 180)
        loaded, requests, seconds = score_feed.count_averages([(0, 10000, 20000), (600, 10, 20), (660, 20, 60)], 660)
        self.assertEqual((loaded, requests, seconds), (10, 20, 60))
        self.assertEqual(score_feed.count_averages([(0, None, None), (60, 3, 4)], 60), (3, 4, 0))

    def test_count_average_window_is_exactly_900_seconds(self) -> None:
        samples = [(840, 10000, 20000)] + [(at, 10, 20) for at in range(900, 1800, 60)] + [(1800, 50, 100)]
        self.assertEqual(score_feed.count_averages(samples, 1800), (10, 20, 900))

    def test_saved_counts_survive_restart_and_all_cached_windows(self) -> None:
        now = 1_700_000_040
        model = "new-counts"
        price = ModelPrice(1, 1)
        self.store.record({model: score_feed.PublicCapacitySample(model, 10, 20, 2, 7)}, {model: price}, price, now)
        self.store.record({model: score_feed.PublicCapacitySample(model, 30, 60, 2, 5)}, {model: price}, price, now + 60)
        self.store = score_feed.ScoreStore(self.store.path)
        service = score_feed.ScoreService(self.store)
        with patch.object(score_feed.time, "time", return_value=now + 60):
            for window in score_feed.WINDOW_BUCKETS:
                first, ttl = service.read_scores(window)
                second, _ = service.read_scores(window)
                self.assertIs(first, second)
                self.assertGreater(ttl, 0)
                latest = json.loads(first)["rows"][-1]
                self.assertEqual((latest["loaded"], latest["requests"], latest["available_to_load"]), (30, 60, 5))
                self.assertEqual((latest["average_loaded"], latest["average_requests"], latest["average_coverage_seconds"]), (10, 20, 60))

    def test_original_schema_upgrade_preserves_scores_without_inventing_counts(self) -> None:
        path = Path(self.directory.name) / "old-schema.sqlite3"
        with closing(sqlite3.connect(path)) as db:
            db.executescript("""
                CREATE TABLE pressure_samples(at INTEGER, model_id TEXT, pressure REAL, PRIMARY KEY(at,model_id));
                CREATE TABLE scores(at INTEGER, model_id TEXT, score REAL, average_pressure REAL,
                  blended_price_usd REAL, model_weight REAL, sample_count INTEGER, PRIMARY KEY(at,model_id));
                INSERT INTO scores VALUES(1700000040,'old-model',0.75,1.5,0.5,1,15);
            """)
        for _ in range(2):
            old = score_feed.ScoreStore(path)
            row = old.read(7200, 1_700_000_040)["rows"][0]
            self.assertEqual(row["score"], 0.75)
            for key in ("loaded", "requests", "average_loaded", "average_requests", "available_to_load"):
                self.assertIsNone(row[key])

    def test_score_cache_reuses_serialized_results_per_window_and_expires(self) -> None:
        now = 1_700_000_040
        service = score_feed.ScoreService(self.store)
        model = "cache-model"
        price = ModelPrice(1, 1)
        self.store.record({model: CapacitySample(model, 1, 2, 2)}, {model: price}, price, now)
        with patch.object(score_feed.time, "time", return_value=now), \
             patch.object(score_feed.time, "monotonic", return_value=100) as clock, \
             patch.object(self.store, "read", wraps=self.store.read) as read:
            first, ttl = service.read_scores(7200)
            self.assertEqual(ttl, 30)
            clock.return_value = 111
            second, ttl = service.read_scores(7200)
            self.assertIs(first, second)
            self.assertEqual(ttl, 19)
            self.assertEqual(read.call_count, 1)
            monthly, _ = service.read_scores(2592000)
            self.assertEqual(json.loads(monthly)["bucket_seconds"], 3600)
            self.assertEqual(read.call_count, 2)
            clock.return_value = 130
            service.read_scores(7200)
            self.assertEqual(read.call_count, 3)
            with self.assertRaises(ValueError):
                service.read_scores(9999)
            self.assertEqual(read.call_count, 3)

    def test_concurrent_score_requests_share_one_database_read(self) -> None:
        service = score_feed.ScoreService(self.store)
        with patch.object(self.store, "read", wraps=self.store.read) as read:
            with ThreadPoolExecutor(max_workers=10) as pool:
                responses = list(pool.map(service.read_scores, [7200] * 20))
            self.assertEqual(read.call_count, 1)
            self.assertTrue(all(body is responses[0][0] for body, _ in responses))
            self.assertTrue(all(ttl == 0 for _, ttl in responses))  # Empty history stays out of the CDN.

    def test_new_sample_invalidates_cached_scores_immediately(self) -> None:
        now = 1_700_000_040
        service = score_feed.ScoreService(self.store)
        model = "cache-model"
        price = ModelPrice(1, 1)
        with patch.object(score_feed, "fetch_capacity", return_value={model: CapacitySample(model, 1, 2, 2)}), \
             patch.object(score_feed, "fetch_model_prices", return_value=({model: price}, price)), \
             patch.object(score_feed.time, "time", return_value=now) as wall_clock:
            service.capture(now)
            body, _ = service.read_scores(7200)
            self.assertEqual(len(json.loads(body)["rows"]), 1)
            service.capture(now + 60)
            wall_clock.return_value = now + 60
            body, _ = service.read_scores(7200)
            self.assertEqual(len(json.loads(body)["rows"]), 2)

    def test_collection_failure_invalidates_cache_and_disables_cdn_storage(self) -> None:
        now = 1_700_000_040
        service = score_feed.ScoreService(self.store)
        model = "cache-model"
        price = ModelPrice(1, 1)
        self.store.record({model: CapacitySample(model, 1, 2, 2)}, {model: price}, price, now)
        with patch.object(score_feed.time, "time", return_value=now):
            _, ttl = service.read_scores(7200)
            self.assertGreater(ttl, 0)
            stop = threading.Event()
            def fail_capture():
                stop.set()
                raise RuntimeError("public feed unavailable")
            with patch.object(service, "capture", side_effect=fail_capture), patch("builtins.print"):
                service.loop(60, stop)
            body, ttl = service.read_scores(7200)
            self.assertEqual(ttl, 0)
            self.assertEqual(json.loads(body)["last_error"], "public feed unavailable")

    def test_records_manager_formula_and_time_windows(self) -> None:
        now = 1_700_000_040
        model_id = "qwen3.5-35b-a3b"
        prices = {model_id: ModelPrice(0.08, 0.75)}
        fallback = ModelPrice(None, None)
        self.store.record({model_id: CapacitySample(model_id, 2, 4, 2.0)}, prices, fallback, now)
        self.store.record({model_id: CapacitySample(model_id, 2, 2, 1.0)}, prices, fallback, now + 60)
        payload = self.store.read(7200, now + 60)
        self.assertEqual(payload["bucket_seconds"], 60)
        self.assertEqual(len(payload["rows"]), 2)
        self.assertAlmostEqual(payload["rows"][-1]["saved_score"], 1.5 * 0.1805 * 1.25)
        self.assertAlmostEqual(payload["rows"][-1]["score"], 2 * 0.1805 * 1.25)
        self.assertEqual(self.store.read(1800, now + 60)["rows"][-1]["model_id"], model_id)
        with self.assertRaises(ValueError):
            self.store.read(9999, now)

    def test_missing_price_stays_null_and_gap_is_not_filled(self) -> None:
        now = 1_700_000_040
        model_id = "new-model"
        sample = CapacitySample(model_id, 1, 3, 3.0)
        self.store.record({model_id: sample}, {}, ModelPrice(None, None), now)
        row = self.store.read(7200, now)["rows"][0]
        self.assertIsNone(row["score"])

    def test_average_excludes_samples_older_than_15_elapsed_minutes(self) -> None:
        now = 1_700_000_040
        model = "test-model"
        price = ModelPrice(1, 1)
        self.store.record({model: CapacitySample(model, 1, 100, 100)}, {model: price}, price, now)
        self.store.record({model: CapacitySample(model, 1, 2, 2)}, {model: price}, price, now + 900)
        rows = self.store.read(7200, now + 900)["rows"]
        self.assertEqual(rows[-1]["score"], 2)
        self.assertEqual(len(rows), 2)

    def test_fresh_database_contains_only_model_history(self) -> None:
        with self.store.connect() as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tables, {"scores", "pressure_samples"})

    def test_dependency_installer_rejects_modified_source(self) -> None:
        target = Path(self.directory.name) / "warm_model_manager.py"
        target.write_text("# modified dependency\n", encoding="utf-8")
        with patch.object(fetch_manager, "TARGET", target), patch.object(fetch_manager, "urlopen") as network:
            with self.assertRaises(RuntimeError):
                fetch_manager.install()
        network.assert_not_called()
        self.assertEqual(target.read_text(), "# modified dependency\n")

    def test_imports_only_public_score_history(self) -> None:
        path = Path(self.directory.name) / "backfill.csv"
        path.write_text(
            "observed_at,model_id,score,average_pressure,blended_price_usd,model_weight,sample_count\n"
            "2023-11-14T22:13:20Z,gpt-oss-20b,0.25,2,0.125,1,15\n",
            encoding="utf-8",
        )
        self.assertEqual(self.store.import_csv(path), 1)
        self.assertEqual(self.store.read(7200, 1_700_000_100)["rows"][0]["score"], 0.25)

    def test_seeds_only_recent_load_samples(self) -> None:
        now = 1_700_000_100
        path = Path(self.directory.name) / "model-load.csv"
        path.write_text(
            "observed_at,model_id,display_name,loaded,in_progress,available_to_load\n"
            "2023-11-14T21:00:00Z,gpt-oss-20b,GPT-OSS,2,4,10\n"
            "2023-11-14T22:13:05Z,gpt-oss-20b,GPT-OSS,2,4,10\n"
            "2023-11-14T22:13:20Z,gpt-oss-20b,GPT-OSS,2,6,10\n",
            encoding="utf-8",
        )
        self.assertEqual(self.store.import_load_csv(path, now), 1)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT pressure FROM pressure_samples").fetchone()[0], 3.0)

    def test_upgrade_removes_legacy_visitors_but_preserves_scores(self) -> None:
        now = 1_700_000_040
        model = "gpt-oss-20b"
        price = ModelPrice(0.02, 0.1)
        self.store.record({model: CapacitySample(model, 2, 4, 2)}, {model: price}, price, now)
        expected = self.store.read(7200, now)
        with self.store.connect() as db:
            db.executescript("""
                CREATE TABLE traffic_events (
                    id INTEGER PRIMARY KEY, at INTEGER, session_id TEXT, ip TEXT,
                    kind TEXT, watched_seconds INTEGER
                );
                CREATE INDEX traffic_events_time ON traffic_events(at);
                CREATE INDEX traffic_events_session ON traffic_events(session_id,at);
                CREATE INDEX traffic_events_ip_time ON traffic_events(ip,at);
                INSERT INTO traffic_events VALUES (1,1700000040,'legacy-session','203.0.113.7','view',0);
                CREATE TABLE unrelated_data (value TEXT);
                INSERT INTO unrelated_data VALUES ('keep me');
            """)
        for _ in range(2):
            self.store = score_feed.ScoreStore(self.store.path)
            self.assertEqual(self.store.read(7200, now), expected)
            with self.store.connect() as db:
                self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'traffic_events%'").fetchall(), [])
                self.assertEqual(db.execute("SELECT COUNT(*) FROM pressure_samples").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT value FROM unrelated_data").fetchone()[0], "keep me")
            self.store.prune(now)

    def test_http_is_score_only_and_rejects_legacy_tracking(self) -> None:
        service = score_feed.ScoreService(self.store)
        server = score_feed.ThreadingHTTPServer(("127.0.0.1", 0), score_feed.ScoreHandler)
        server.service = service
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"
        with urlopen(base + "/", timeout=3) as response:
            html = response.read().decode()
            self.assertIn("Darkbloom model scores", html)
            for marker in ("Website traffic", "api/traffic", "api/view", "api/heartbeat", "sessionStorage", "localStorage", "sendTraffic"):
                self.assertNotIn(marker, html)
            csp = response.headers["Content-Security-Policy"]
            self.assertIn("https://static.cloudflareinsights.com;", csp)
            self.assertIn("connect-src 'self'", csp)
            self.assertNotIn("no-transform", response.headers["Cache-Control"])
            self.assertIsNone(response.headers.get("Set-Cookie"))
        with urlopen(base + "/api/scores?window=7200", timeout=3) as response:
            payload = json.load(response)
            self.assertEqual(payload["rows"], [])
            self.assertEqual(payload["average_seconds"], 1800)
        with urlopen(base + "/api/scores?window=1800&average=604800", timeout=3) as response:
            payload = json.load(response)
            self.assertEqual((payload["window_seconds"], payload["average_seconds"]), (1800, 604800))
        with self.assertRaises(HTTPError) as invalid_average:
            urlopen(base + "/api/scores?window=7200&average=99999999", timeout=3)
        self.assertEqual(invalid_average.exception.code, 400)
        self.assertEqual(invalid_average.exception.headers["Cache-Control"], "no-store")
        invalid_average.exception.close()
        with urlopen(base + "/model-charts.js", timeout=3) as response:
            self.assertIn("text/javascript", response.headers["Content-Type"])
            self.assertIn(b"Loaded models", response.read())
        with urlopen(base + "/healthz", timeout=3) as response:
            self.assertTrue(json.load(response)["ok"])
            self.assertEqual(response.headers["Cache-Control"], "no-store")
        model = "cache-model"
        price = ModelPrice(1, 1)
        with patch.object(score_feed, "fetch_capacity", return_value={model: CapacitySample(model, 1, 2, 2)}), \
             patch.object(score_feed, "fetch_model_prices", return_value=({model: price}, price)):
            service.capture()
        with urlopen(base + "/api/scores?window=7200", timeout=3) as response:
            self.assertIn("public, max-age=0, s-maxage=", response.headers["Cache-Control"])
            self.assertEqual(len(json.load(response)["rows"]), 1)
        with self.assertRaises(HTTPError) as error:
            urlopen(base + "/api/scores?window=9999", timeout=3)
        self.assertEqual(error.exception.code, 400)
        self.assertEqual(error.exception.headers["Cache-Control"], "no-store")
        error.exception.close()
        for path in ("/.env", "/data/scores.sqlite3", "/server.py", "/api/earnings", "/api/providers", "/api/traffic", "/api/view", "/api/heartbeat"):
            with self.assertRaises(HTTPError) as error:
                urlopen(base + path, timeout=3)
            self.assertEqual(error.exception.code, 404)
            error.exception.close()
        for path in ("/api/traffic", "/api/view", "/api/heartbeat"):
            request = Request(
                base + path, data=b'{"session_id":"legacy_browser_session"}', method="POST",
                headers={"Content-Type": "application/json", "X-Real-IP": "203.0.113.9",
                         "X-Forwarded-For": "198.51.100.2", "Origin": "https://other.example"},
            )
            with self.assertRaises(HTTPError) as error:
                urlopen(request, timeout=3)
            self.assertEqual(error.exception.code, 404)
            error.exception.close()
        self.test_fresh_database_contains_only_model_history()

    def test_collector_uses_only_public_capacity_and_pricing(self) -> None:
        service = score_feed.ScoreService(self.store)
        model = "gpt-oss-20b"
        with patch.object(score_feed, "fetch_capacity", return_value={model: CapacitySample(model, 2, 4, 2.0)}) as capacity:
            with patch.object(score_feed, "fetch_model_prices", return_value=({model: ModelPrice(0.02, 0.1)}, ModelPrice(None, None))) as pricing:
                self.assertEqual(service.capture(1_700_000_040), 1)
        capacity.assert_called_once_with(score_feed.DEFAULT_BASE_URL)
        pricing.assert_called_once_with(score_feed.DEFAULT_PRICING_URL)
        self.assertAlmostEqual(self.store.read(7200, 1_700_000_040)["rows"][0]["score"], 0.064)


if __name__ == "__main__":
    unittest.main()
