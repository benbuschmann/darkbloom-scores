"""Offline checks for the public score-only site and traffic counters."""

import json
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
        self.assertAlmostEqual(payload["rows"][-1]["score"], 1.5 * 0.1805 * 1.25)
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

    def test_proxy_headers_are_ignored_unless_peer_is_trusted(self) -> None:
        service = score_feed.ScoreService(self.store)
        self.assertEqual(service.client_ip("203.0.113.9", "198.51.100.5", None), "203.0.113.9")
        service = score_feed.ScoreService(self.store, "172.20.0.0/24", "https://scores.example")
        self.assertEqual(service.client_ip("172.20.0.2", "198.51.100.99, 203.0.113.9", None), "203.0.113.9")
        self.assertEqual(service.client_ip("172.20.0.2", "203.0.113.9, 172.20.0.3", None), "203.0.113.9")
        self.assertEqual(service.client_ip("172.20.0.2", "not-an-address", None), "172.20.0.2")
        with self.assertRaises(ValueError):
            score_feed.ScoreService(self.store, public_origin="https://scores.example/path")

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

    def test_traffic_counts_active_sessions_and_drops_old_ips(self) -> None:
        now = 1_700_000_000
        session = "session_12345"
        self.store.record_traffic("view", session, "203.0.113.7", now)
        self.store.record_traffic("heartbeat", session, "203.0.113.7", now)
        self.store.record_traffic("heartbeat", session, "203.0.113.7", now + 60)
        self.store.record_traffic("heartbeat", session, "203.0.113.7", now + 61)
        summary = self.store.traffic_summary(now + 61)
        self.assertEqual(summary["active_viewers"], 1)
        self.assertEqual(summary["page_views_24h"], 1)
        self.assertEqual(summary["unique_ips_24h"], 1)
        self.assertEqual(summary["viewer_hours_24h"], 0.02)
        with self.assertRaises(ValueError):
            self.store.record_traffic("view", "bad", "203.0.113.7", now)
        later = now + 8 * 86400
        self.store.record_traffic("view", "another_session", "198.51.100.2", later)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM traffic_events").fetchone()[0], 1)
        self.store.prune(later + 8 * 86400)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM traffic_events").fetchone()[0], 0)

    def test_http_site_is_public_score_only_and_traffic_is_aggregate(self) -> None:
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
            self.assertIn(b"Darkbloom model scores", response.read())
        with urlopen(base + "/api/scores?window=7200", timeout=3) as response:
            self.assertEqual(json.load(response)["rows"], [])
        for path in ("/healthz", "/api/traffic"):
            with urlopen(base + path, timeout=3) as response:
                self.assertEqual(response.status, 200)
        for path in ("/.env", "/data/scores.sqlite3", "/server.py", "/api/earnings", "/api/providers"):
            with self.assertRaises(HTTPError) as error:
                urlopen(base + path, timeout=3)
            self.assertEqual(error.exception.code, 404)
            error.exception.close()
        body = json.dumps({"session_id": "browser_session_123"}).encode()
        request = Request(
            base + "/api/view", data=body, method="POST",
            headers={"Content-Type": "application/json", "X-Real-IP": "203.0.113.9"},
        )
        with urlopen(request, timeout=3) as response:
            self.assertTrue(json.load(response)["ok"])
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT ip FROM traffic_events").fetchone()[0], "127.0.0.1")
        service.public_origin = "https://scores.example"
        secure_origin = Request(
            base + "/api/heartbeat", data=body, method="POST",
            headers={"Content-Type": "application/json", "Origin": "https://scores.example"},
        )
        with urlopen(secure_origin, timeout=3) as response:
            self.assertTrue(json.load(response)["ok"])
        cross_origin = Request(
            base + "/api/view", data=body, method="POST",
            headers={"Content-Type": "application/json", "Origin": "https://other.example"},
        )
        with self.assertRaises(HTTPError) as error:
            urlopen(cross_origin, timeout=3)
        self.assertEqual(error.exception.code, 400)
        error.exception.close()
        with urlopen(base + "/api/traffic", timeout=3) as response:
            payload = json.load(response)
            self.assertEqual(payload["page_views_24h"], 1)
            self.assertEqual(payload["unique_ips_24h"], 1)
            self.assertNotIn("203.0.113.9", json.dumps(payload))

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
