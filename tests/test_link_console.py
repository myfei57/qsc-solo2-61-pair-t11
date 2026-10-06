"""End to end tests for link monitoring and degraded running over HTTP."""

from __future__ import annotations

import json
import tempfile
import unittest

from tunnelnet.console.server import Server
from tunnelnet.store import Store


class LinkConsoleCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store.open(f"{self._tmp.name}/data")
        self.server = Server(self.store, web_dir="web")
        # Tighten the seeded link policy for a deterministic test window.
        self._set_policy(
            "link-main",
            interval=10.0,
            miss_threshold=3,
            recover_hits=2,
            offline_ratio=0.5,
            recover_stable_seconds=30.0,
        )

    def _set_policy(self, link_id, **fields) -> None:
        monitor = self.server.runtime.link_monitor
        link = monitor.find_link(link_id)
        for key, value in fields.items():
            setattr(link, key, value)
        from tunnelnet.link.persistence import save_link
        save_link(self.store, link)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def call(self, method, path, payload=None, query=None):
        response = self.server.dispatch(method, path, payload, query)
        body = response.body.decode("utf-8")
        parsed = json.loads(body) if response.content_type.startswith("application/json") else body
        return response.status, parsed

    def test_seeded_report_is_online_with_devices(self) -> None:
        status, report = self.call("GET", "/api/link")
        self.assertEqual(status, 200)
        self.assertEqual(report["mode"], "online")
        self.assertEqual(report["devices"]["total"], 3)
        self.assertEqual(len(report["links"]), 1)
        self.assertEqual(report["paused"], [])

        # Devices that never heartbeated are UNKNOWN: online mode but not green.
        _, checks = self.call("GET", "/api/health/checks")
        link_check = next(c for c in checks["checks"] if c["name"] == "link")
        self.assertEqual(link_check["status"], "warn")
        self.assertIn("unknown=3", link_check["detail"])

        status, linkages = self.call("GET", "/api/link/linkages")
        self.assertEqual(status, 200)
        self.assertEqual(linkages["mode"], "online")
        self.assertIn("access.release", [item["key"] for item in linkages["items"]])

    def test_heartbeat_sweep_degrade_confirm_rollback(self) -> None:
        clock = self.server.runtime.link_clock
        clock.set(0.0)
        # Bring all devices online at t=0.
        for device in ("collector-1", "collector-2", "collector-3"):
            self.call("POST", "/api/link/heartbeat", {"device_id": device})

        # One miss -> jitter, still online.
        clock.set(12.0)
        _, sweep = self.call("POST", "/api/link/sweep")
        self.assertEqual(sweep["mode"], "online")

        # Three misses -> offline, 3/3 >= 0.5 -> degraded with paused linkages.
        clock.set(32.0)
        status, sweep = self.call("POST", "/api/link/sweep")
        self.assertEqual(status, 200)
        self.assertEqual(sweep["mode"], "degraded")
        self.assertIn("access.release", sweep["paused"])
        self.assertNotIn("fire.suppression", sweep["paused"])

        # Health check reports the link as down, so the console can't lie green.
        _, checks = self.call("GET", "/api/health/checks")
        link_check = next(c for c in checks["checks"] if c["name"] == "link")
        self.assertEqual(link_check["status"], "down")

        # A normal linkage is reported blocked; safety stays allowed.
        _, gate = self.call("GET", "/api/link/linkages", query={"key": ["access.release"]})
        self.assertFalse(gate["allowed"])
        _, gate = self.call("GET", "/api/link/linkages", query={"key": ["fire.suppression"]})
        self.assertTrue(gate["allowed"])

        # Devices back with two consecutive heartbeats -> recovering (still paused).
        clock.set(33.0)
        for device in ("collector-1", "collector-2", "collector-3"):
            self.call("POST", "/api/link/heartbeat", {"device_id": device})
        clock.set(34.0)
        for device in ("collector-1", "collector-2", "collector-3"):
            self.call("POST", "/api/link/heartbeat", {"device_id": device})
        _, report = self.call("GET", "/api/link")
        self.assertEqual(report["mode"], "recovering")
        self.assertTrue(any(item["paused"] for item in report["paused"]))

        # Confirm before stable window elapses -> 409.
        clock.set(35.0)
        status, body = self.call("POST", "/api/link/recover/confirm", {})
        self.assertEqual(status, 409)
        self.assertIn("stable window", body["error"])

        # After 30s of stability confirm -> online, linkages resumed.
        clock.set(70.0)
        status, body = self.call("POST", "/api/link/recover/confirm", {})
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], "online")
        self.assertIn("access.release", body["restored"])
        _, report = self.call("GET", "/api/link")
        self.assertEqual(report["mode"], "online")

        # Confirm again is idempotent.
        status, again = self.call("POST", "/api/link/recover/confirm", {})
        self.assertEqual(status, 200)
        self.assertEqual(again["restored"], [])

        # Rollback re-enters degraded.
        status, rolled = self.call("POST", "/api/link/recover/rollback", {})
        self.assertEqual(status, 200)
        self.assertTrue(rolled["rolled_back"])
        self.assertEqual(rolled["state"], "degraded")
        status, rolled = self.call("POST", "/api/link/recover/rollback", {})
        self.assertTrue(rolled["rolled_back"] is False)

    def test_events_and_shared_audit_trail(self) -> None:
        for device in ("collector-1", "collector-2", "collector-3"):
            self.call("POST", "/api/link/heartbeat", {"device_id": device, "at": 0.0})
        self.call("POST", "/api/link/sweep", {"at": 32.0})

        status, events = self.call("GET", "/api/link/events")
        self.assertEqual(status, 200)
        names = [e["new"] for e in events["items"] if e["scope"] == "link"]
        self.assertIn("degraded", names)

        # The same transitions are mirrored into the console audit trail.
        _, audit = self.call("GET", "/api/audit/query", query={"event": ["link_degraded"]})
        self.assertEqual(len(audit["filtered"]), 1)

    def test_control_cycle_skips_paused_linkages_but_keeps_safety(self) -> None:
        clock = self.server.runtime.link_clock
        clock.set(0.0)
        for device in ("collector-1", "collector-2", "collector-3"):
            self.call("POST", "/api/link/heartbeat", {"device_id": device})
        clock.set(40.0)
        self.call("POST", "/api/link/sweep")
        self.assertEqual(self.call("GET", "/api/link")[1]["mode"], "degraded")

        status, body = self.call(
            "POST",
            "/api/cycle",
            {
                "cabin_id": "cabin-1",
                "requester": "ops",
                "zone": "zone-1",
                "sensor_id": "sensor-1",
                "at": "t",
                "watts": 100.0,
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["link_mode"], "degraded")
        # Ordinary linkages were not driven.
        self.assertIsNone(body["approval"])
        self.assertFalse(body["alarmed"])
        self.assertEqual(body["usage"], 0.0)
        self.assertIn("access.release", body["skipped_linkages"])
        self.assertIn("env.alarm", body["skipped_linkages"])
        self.assertIn("drain.pump", body["skipped_linkages"])
        # Safety linkage still ran and the cycle was audited.
        self.assertEqual(body["zones"], ["zone-1", "zone-2", "zone-3"])
        self.assertTrue(body["alarm_raised"])
        self.assertEqual(body["last_stage"], "audit")

    def test_register_device_and_link(self) -> None:
        status, body = self.call(
            "POST", "/api/link/register", {"link_id": "link-b", "name": "branch"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["link"]["id"], "link-b")
        status, body = self.call(
            "POST", "/api/link/register", {"device_id": "collector-9", "on": "link-b"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["device"]["link_id"], "link-b")

        status, body = self.call("POST", "/api/link/heartbeat", {"device_id": "ghost", "at": 1.0})
        self.assertEqual(status, 404)

    def test_link_state_persists_across_server_restart(self) -> None:
        for device in ("collector-1", "collector-2", "collector-3"):
            self.call("POST", "/api/link/heartbeat", {"device_id": device, "at": 0.0})
        self.call("POST", "/api/link/sweep", {"at": 32.0})

        restarted = Server(self.store, web_dir="web")
        response = restarted.dispatch("GET", "/api/link")
        report = json.loads(response.body.decode("utf-8"))
        self.assertEqual(report["mode"], "degraded")
        self.assertIn("access.release", [p["key"] for p in report["paused"]])


if __name__ == "__main__":
    unittest.main()
