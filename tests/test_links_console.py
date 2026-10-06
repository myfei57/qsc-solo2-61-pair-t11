"""End to end tests for the link monitoring console endpoints."""

from __future__ import annotations

import json
import tempfile
import unittest

from tunnelnet.console.server import Server
from tunnelnet.store import Store


class LinkConsoleCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store.open(f"{self.tmp.name}/data")
        self.server = Server(self.store, web_dir="web")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def call(self, method: str, path: str, payload: dict | None = None, query: dict | None = None):
        response = self.server.dispatch(method, path, payload, query)
        body = json.loads(response.body.decode("utf-8"))
        return response.status, body

    def test_seeded_link_is_bootstrapped_degraded_and_persisted(self) -> None:
        status, overview = self.call("GET", "/api/links")
        self.assertEqual(status, 200)
        (entry,) = overview["links"]
        self.assertEqual(entry["state"]["mode"], "degraded")
        self.assertEqual(entry["link"]["id"], "uplink-main")
        self.assertEqual(len(entry["devices"]), 3)
        self.assertTrue(self.store.list("link-state"))
        self.assertTrue(self.store.list("link-device"))
        self.assertTrue(self.store.list("link-linkage"))

    def test_policy_endpoint_documents_the_decisions(self) -> None:
        status, policy = self.call("GET", "/api/links/policy")
        self.assertEqual(status, 200)
        self.assertIn("miss_limit", policy["offline_policy"])
        self.assertIn("stable_ticks", policy["recovery_policy"])

    def test_heartbeats_recover_the_link_and_gates_resume(self) -> None:
        seq = 0
        for step in range(6):
            t = step * 10
            for device in ("collector-1", "collector-2", "collector-3"):
                seq += 1
                status, _ = self.call(
                    "POST",
                    "/api/links/heartbeat",
                    {"device_id": device, "seq": seq, "at": t},
                )
                self.assertEqual(status, 200)
        status, overview = self.call("GET", "/api/links")
        state = overview["links"][0]["state"]
        self.assertEqual(state["mode"], "normal")
        gates = {row["id"]: row for row in overview["links"][0]["linkages"]}
        self.assertEqual(gates["drain-auto"]["actual"], "active")
        # fail-safe linkage is active even before recovery
        self.assertEqual(gates["gas-alarm"]["actual"], "active")

    def test_outage_pauses_linkages_and_audit_shows_the_same_data(self) -> None:
        # recover first
        seq = 0
        for step in range(6):
            for device in ("collector-1", "collector-2", "collector-3"):
                seq += 1
                self.call("POST", "/api/links/heartbeat", {"device_id": device, "seq": seq, "at": step * 10})
        # then silence all collectors
        for t in (100, 110, 120):
            self.call("POST", "/api/links/tick", {"link_id": "uplink-main", "at": t})
        status, overview = self.call("GET", "/api/links")
        self.assertEqual(overview["links"][0]["state"]["mode"], "degraded")
        gates = {row["id"]: row["actual"] for row in overview["links"][0]["linkages"]}
        self.assertEqual(gates["drain-auto"], "paused")
        self.assertEqual(gates["vent-auto"], "paused")
        self.assertEqual(gates["gas-alarm"], "active")

        status, audit = self.call("GET", "/api/links/audit", query={"entity_type": "linkage"})
        self.assertEqual(status, 200)
        pause_events = {
            event["entity_id"]
            for event in audit["events"]
            if event["kind"] == "linkage_pause_planned" and event["at"] >= 100
        }
        self.assertEqual(pause_events, {"drain-auto", "vent-auto"})

        status, commands = self.call("GET", "/api/links/commands")
        acked = [c for c in commands["commands"] if c["status"] == "acked"]
        self.assertTrue(any(c["linkage_id"] == "drain-auto" and c["action"] == "pause" for c in acked))

    def test_hold_blocks_auto_recovery_until_release(self) -> None:
        seq = 0
        for step in range(6):
            for device in ("collector-1", "collector-2", "collector-3"):
                seq += 1
                self.call("POST", "/api/links/heartbeat", {"device_id": device, "seq": seq, "at": step * 10})
        status, held = self.call(
            "POST", "/api/links/hold", {"link_id": "uplink-main", "reason": "crew on site"}
        )
        self.assertEqual(status, 200)
        self.assertTrue(held["link"]["hold"])
        for step in range(6, 12):
            for device in ("collector-1", "collector-2", "collector-3"):
                seq += 1
                self.call("POST", "/api/links/heartbeat", {"device_id": device, "seq": seq, "at": step * 10})
        _, overview = self.call("GET", "/api/links")
        self.assertEqual(overview["links"][0]["state"]["mode"], "degraded")
        status, released = self.call("POST", "/api/links/release", {"link_id": "uplink-main"})
        self.assertFalse(released["link"]["hold"])

    def test_cycle_is_gated_by_link_mode(self) -> None:
        cycle_payload = {
            "cabin_id": "cabin-1",
            "requester": "ops",
            "zone": "zone-1",
            "sensor_id": "sensor-1",
            "samples": [10.0, 20.0],
            "at": "2026-02-01T00:00:00Z",
            "watts": 100.0,
        }
        # degraded at boot: drain linkage withheld, fire alarm still raised
        _, body = self.call("POST", "/api/cycle", cycle_payload)
        self.assertTrue(body["degraded"])
        self.assertEqual(body["drain_runs"], 0)
        self.assertTrue(body["alarm_raised"])

        # prove the link healthy
        seq = 0
        for step in range(8):
            for device in ("collector-1", "collector-2", "collector-3"):
                seq += 1
                self.call("POST", "/api/links/heartbeat", {"device_id": device, "seq": seq, "at": step * 10})
        _, body = self.call("POST", "/api/cycle", cycle_payload)
        self.assertFalse(body["degraded"])
        self.assertEqual(body["paused_linkages"], [])
        self.assertEqual(body["drain_runs"], 1)

    def test_invalid_heartbeat_is_rejected(self) -> None:
        status, body = self.call("POST", "/api/links/heartbeat", {"device_id": "collector-1", "seq": 0})
        self.assertEqual(status, 400)
        self.assertIn("seq", body["error"])

    def test_registering_custom_link_device_and_linkage(self) -> None:
        status, _ = self.call("POST", "/api/links/register", {"id": "uplink-b", "miss_limit": 2})
        self.assertEqual(status, 200)
        status, _ = self.call(
            "POST", "/api/links/devices/register", {"id": "b1", "link_id": "uplink-b", "critical": True}
        )
        self.assertEqual(status, 200)
        status, _ = self.call(
            "POST", "/api/links/linkages/register", {"id": "b-pump", "link_id": "uplink-b"}
        )
        self.assertEqual(status, 200)
        _, overview = self.call("GET", "/api/links")
        ids = {entry["link"]["id"] for entry in overview["links"]}
        self.assertEqual(ids, {"uplink-main", "uplink-b"})


if __name__ == "__main__":
    unittest.main()
