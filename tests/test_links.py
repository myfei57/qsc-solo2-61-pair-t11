"""Tests for link online monitoring and degraded operation."""

from __future__ import annotations

import tempfile
import unittest

from tunnelnet.link import (
    ACTION_PAUSE,
    ACTION_RESUME,
    CMD_ACKED,
    GATE_ACTIVE,
    GATE_PAUSED,
    JITTER,
    LINK_DOWN,
    LINK_UP,
    MODE_DEGRADED,
    MODE_NORMAL,
    MODE_RECOVERING,
    OFFLINE,
    ONLINE,
    SUSPECT,
    DeviceConfig,
    LinkConfig,
    LinkMonitor,
    LinkRepository,
    Linkage,
    RecordingActuator,
)
from tunnelnet.link.model import DeviceState, LinkState
from tunnelnet.store import Store


def build_monitor(root: str, stable_ticks: int = 2, miss_limit: int = 3) -> tuple[LinkMonitor, RecordingActuator]:
    repo = LinkRepository(Store.open(root))
    actuator = RecordingActuator()
    monitor = LinkMonitor(repo, actuator, clock=_Clock())
    monitor.ensure_link(
        LinkConfig(
            id="L",
            miss_limit=miss_limit,
            heartbeat_period=10,
            flap_window=60,
            flap_limit=3,
            offline_ratio=0.34,
            stable_ticks=stable_ticks,
        )
    )
    return monitor, actuator


class _Clock:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self) -> int:
        self.value += 1
        return self.value


def settle(monitor: LinkMonitor, devices: list[str], *, start: int = 0, count: int = 4) -> None:
    """Send regular heartbeats long enough to reach NORMAL."""

    seq = 0
    for step in range(count):
        t = start + step * 10
        for device in devices:
            seq += 1
            monitor.heartbeat(device, seq, at=t)
        monitor.tick("L", at=t + 5)


class DeviceVerdictCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.monitor, _ = build_monitor(self.tmp.name)
        self.monitor.ensure_device(DeviceConfig(id="d", link_id="L"))
        for seq in range(1, 5):
            self.monitor.heartbeat("d", seq, at=seq * 10)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_one_missed_heartbeat_is_suspect_not_offline(self) -> None:
        state = self.monitor.tick("L", at=51)
        device = self.monitor.snapshot()["links"][0]["devices"][0]
        self.assertEqual(device["status"], SUSPECT)
        self.assertEqual(state.availability, LINK_UP)

    def test_consecutive_misses_confirm_offline(self) -> None:
        for t in (51, 61, 71):
            self.monitor.tick("L", at=t)
        device = self.monitor.snapshot()["links"][0]["devices"][0]
        self.assertEqual(device["status"], OFFLINE)
        self.assertEqual(device["missed"], 3)

    def test_heartbeat_in_suspect_restores_online_without_churning_link(self) -> None:
        # device is healthy and the link is NORMAL after setUp beats
        normal_events = [e for e in self.monitor.audit_trail(entity_type="link") if e["kind"] == "mode_normal"]
        self.assertTrue(normal_events)
        normal_at = normal_events[-1]["at"]
        self.monitor.tick("L", at=51)  # exactly one overdue heartbeat -> suspect
        self.assertEqual(self.monitor.snapshot()["links"][0]["devices"][0]["status"], SUSPECT)
        self.assertEqual(self.monitor.snapshot()["links"][0]["state"]["mode"], MODE_NORMAL)
        self.monitor.heartbeat("d", 99, at=55)
        self.assertEqual(self.monitor.snapshot()["links"][0]["devices"][0]["status"], ONLINE)
        self.assertFalse(
            [
                e
                for e in self.monitor.audit_trail(entity_type="link")
                if e["kind"] == "mode_degraded" and e["at"] >= normal_at
            ]
        )

    def test_duplicate_sequence_is_ignored(self) -> None:
        before = self.monitor.snapshot()["links"][0]["devices"][0]
        self.monitor.heartbeat("d", 4, at=999)
        after = self.monitor.snapshot()["links"][0]["devices"][0]
        self.assertEqual(before["last_heartbeat_at"], after["last_heartbeat_at"])

    def test_repeated_short_drops_become_jitter(self) -> None:
        # three healthy->missing edges inside the 60s window: 50, 70 and 90
        self.monitor.tick("L", at=50)
        self.monitor.heartbeat("d", 101, at=50)
        self.monitor.tick("L", at=60)
        self.monitor.tick("L", at=70)
        self.monitor.heartbeat("d", 102, at=70)
        self.monitor.tick("L", at=80)
        self.monitor.tick("L", at=90)
        device = self.monitor.snapshot()["links"][0]["devices"][0]
        self.assertEqual(device["status"], JITTER)

    def test_jitter_clears_after_a_quiet_window(self) -> None:
        self.monitor.tick("L", at=50)
        self.monitor.heartbeat("d", 101, at=50)
        self.monitor.tick("L", at=60)
        self.monitor.tick("L", at=70)
        self.monitor.heartbeat("d", 102, at=70)
        self.monitor.tick("L", at=80)
        self.monitor.tick("L", at=90)
        self.assertEqual(self.monitor.snapshot()["links"][0]["devices"][0]["status"], JITTER)
        # continuous healthy heartbeats, 60s after the last drop at t=90
        seq = 500
        for t in range(100, 160, 10):
            seq += 1
            self.monitor.heartbeat("d", seq, at=t)
        self.assertEqual(self.monitor.snapshot()["links"][0]["devices"][0]["status"], ONLINE)


class DegradationCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.monitor, self.actuator = build_monitor(self.tmp.name)
        for device in ("d1", "d2", "d3"):
            self.monitor.ensure_device(DeviceConfig(id=device, link_id="L", critical=(device == "d1")))
        self.monitor.ensure_linkage(Linkage(id="pump", link_id="L"))
        self.monitor.ensure_linkage(Linkage(id="vent", link_id="L"))
        self.monitor.ensure_linkage(Linkage(id="gas", link_id="L", fail_safe=True))
        settle(self.monitor, ["d1", "d2", "d3"])

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _gates(self) -> dict[str, tuple[str, str]]:
        linkages = self.monitor.snapshot()["links"][0]["linkages"]
        return {row["id"]: (row["actual"], row["desired"]) for row in linkages}

    def test_normal_mode_leaves_every_gate_active(self) -> None:
        link = self.monitor.snapshot()["links"][0]["state"]
        self.assertEqual(link["mode"], MODE_NORMAL)
        for actual, desired in self._gates().values():
            self.assertEqual(actual, GATE_ACTIVE)
            self.assertEqual(desired, GATE_ACTIVE)

    def test_link_down_pauses_ordinary_linkages_but_not_fail_safe(self) -> None:
        for t in (61, 71, 81):
            self.monitor.tick("L", at=t)
        link = self.monitor.snapshot()["links"][0]["state"]
        self.assertEqual(link["availability"], LINK_DOWN)
        self.assertEqual(link["mode"], MODE_DEGRADED)
        self.assertTrue(self.actuator.is_paused("pump"))
        self.assertTrue(self.actuator.is_paused("vent"))
        self.assertFalse(self.actuator.is_paused("gas"))
        self.assertEqual(self._gates()["gas"], (GATE_ACTIVE, GATE_ACTIVE))
        # the degraded verdict for this outage precedes its pause commands
        degraded_events = [
            e
            for e in self.monitor.audit_trail(entity_type="link")
            if e["kind"] == "mode_degraded" and e["at"] >= 60
        ]
        self.assertTrue(degraded_events)
        degraded_at = degraded_events[-1]["at"]
        for command in self.monitor.commands("L"):
            if command["action"] == ACTION_PAUSE and command["issued_at"] >= 60:
                self.assertGreaterEqual(command["issued_at"], degraded_at)

    def test_critical_device_loss_fails_link_on_its_own(self) -> None:
        # only d1 goes silent; d2/d3 keep beating -> ratio 1/3 below threshold
        seq = 900
        for step in range(4):
            t = 60 + step * 10
            seq += 1
            self.monitor.heartbeat("d2", seq, at=t)
            seq += 1
            self.monitor.heartbeat("d3", seq, at=t)
            self.monitor.tick("L", at=t + 5)
        link = self.monitor.snapshot()["links"][0]["state"]
        self.assertEqual(link["availability"], LINK_DOWN)
        self.assertIn("critical", link["reason"])
        self.assertTrue(self.actuator.is_paused("pump"))

    def test_verdict_then_persist_then_act_ordering(self) -> None:
        for t in (61, 71, 81):
            self.monitor.tick("L", at=t)
        commands = self.monitor.commands("L")
        self.assertTrue(commands)
        for command in commands:
            if command["action"] == ACTION_PAUSE:
                self.assertEqual(command["status"], CMD_ACKED)

    def test_recovery_is_automatic_after_stable_window(self) -> None:
        for t in (61, 71, 81):
            self.monitor.tick("L", at=t)
        self.assertTrue(self.actuator.is_paused("pump"))
        seq = 2000
        for step in range(4):
            t = 90 + step * 10
            for device in ("d1", "d2", "d3"):
                seq += 1
                self.monitor.heartbeat(device, seq, at=t)
        link = self.monitor.snapshot()["links"][0]["state"]
        self.assertEqual(link["mode"], MODE_NORMAL)
        self.assertFalse(self.actuator.is_paused("pump"))
        self.assertFalse(self.actuator.is_paused("vent"))
        self.assertEqual(self._gates()["pump"], (GATE_ACTIVE, GATE_ACTIVE))


class RecoveryControlCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.monitor, self.actuator = build_monitor(self.tmp.name, stable_ticks=2)
        self.monitor.ensure_device(DeviceConfig(id="d1", link_id="L"))
        self.monitor.ensure_device(DeviceConfig(id="d2", link_id="L"))
        self.monitor.ensure_device(DeviceConfig(id="d3", link_id="L"))
        self.monitor.ensure_linkage(Linkage(id="pump", link_id="L"))
        settle(self.monitor, ["d1", "d2", "d3"])
        for t in (61, 71, 81):
            self.monitor.tick("L", at=t)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_hold_keeps_the_link_degraded_even_when_healthy(self) -> None:
        self.monitor.hold("L", reason="maintenance window")
        seq = 3000
        for step in range(5):
            t = 90 + step * 10
            for device in ("d1", "d2", "d3"):
                seq += 1
                self.monitor.heartbeat(device, seq, at=t)
        link = self.monitor.snapshot()["links"][0]["state"]
        self.assertTrue(link["hold"])
        self.assertEqual(link["mode"], MODE_DEGRADED)
        self.assertTrue(self.actuator.is_paused("pump"))

    def test_release_allows_automatic_recovery(self) -> None:
        self.monitor.hold("L")
        seq = 3100
        for step in range(4):
            t = 90 + step * 10
            for device in ("d1", "d2", "d3"):
                seq += 1
                self.monitor.heartbeat(device, seq, at=t)
        self.monitor.release_hold("L")
        for step in range(3):
            t = 140 + step * 10
            for device in ("d1", "d2", "d3"):
                seq += 1
                self.monitor.heartbeat(device, seq, at=t)
        self.assertEqual(self.monitor.snapshot()["links"][0]["state"]["mode"], MODE_NORMAL)

    def test_manual_recover_clears_hold_and_recovers(self) -> None:
        self.monitor.hold("L")
        seq = 3200
        for step in range(5):
            t = 90 + step * 10
            for device in ("d1", "d2", "d3"):
                seq += 1
                self.monitor.heartbeat(device, seq, at=t)
        state = self.monitor.recover("L", actor="ops-chief")
        self.assertFalse(state.hold)
        for step in range(2):
            t = 150 + step * 10
            for device in ("d1", "d2", "d3"):
                seq += 1
                self.monitor.heartbeat(device, seq, at=t)
        self.assertEqual(self.monitor.snapshot()["links"][0]["state"]["mode"], MODE_NORMAL)

    def test_release_without_hold_is_idempotent(self) -> None:
        link = self.monitor.snapshot()["links"][0]["state"]
        state = self.monitor.release_hold("L")
        self.assertEqual(state.mode, link["mode"])


class RollbackAndIdempotencyCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.monitor, self.actuator = build_monitor(self.tmp.name, stable_ticks=2)
        self.monitor.ensure_device(DeviceConfig(id="d", link_id="L"))
        self.monitor.ensure_linkage(Linkage(id="pump", link_id="L"))
        settle(self.monitor, ["d"], count=4)
        for t in (61, 71, 81):
            self.monitor.tick("L", at=t)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _recover_beats(self, start: int, count: int) -> None:
        seq = 4000
        for step in range(count):
            seq += 1
            self.monitor.heartbeat("d", seq, at=start + step * 10)

    def test_repeated_recovery_is_idempotent(self) -> None:
        before = len(self.monitor.commands("L"))
        for t in (200, 200, 200):
            self.monitor.tick("L", at=t)
        self.assertEqual(len(self.monitor.commands("L")), before)

    def test_failed_resume_rolls_the_link_back(self) -> None:
        self.actuator.fail_next(ACTION_RESUME, "pump")
        self._recover_beats(90, 8)
        link = self.monitor.snapshot()["links"][0]["state"]
        self.assertEqual(link["mode"], MODE_DEGRADED)
        gate = self.monitor.snapshot()["links"][0]["linkages"][0]
        self.assertEqual(gate["desired"], GATE_PAUSED)
        rollbacks = [e for e in self.monitor.audit_trail() if e["kind"] == "mode_rollback"]
        self.assertTrue(rollbacks)

    def test_fencing_token_rejects_stale_resume(self) -> None:
        token_before = self.monitor.snapshot()["links"][0]["state"]["fencing_token"]
        self.actuator.fail_next(ACTION_RESUME, "pump")
        self._recover_beats(90, 8)
        token_after = self.monitor.snapshot()["links"][0]["state"]["fencing_token"]
        self.assertGreater(token_after, token_before)
        with self.assertRaises(Exception):
            self.actuator.resume("L", "pump", token=token_before)

    def test_successful_recovery_after_failure(self) -> None:
        self.actuator.fail_next(ACTION_RESUME, "pump")
        self._recover_beats(90, 8)
        self.assertEqual(self.monitor.snapshot()["links"][0]["state"]["mode"], MODE_DEGRADED)
        self.actuator.clear_faults()
        self._recover_beats(200, 8)
        link = self.monitor.snapshot()["links"][0]["state"]
        self.assertEqual(link["mode"], MODE_NORMAL)
        self.assertFalse(self.actuator.is_paused("pump"))


class PersistenceCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.monitor, self.actuator = build_monitor(self.tmp.name)
        self.monitor.ensure_device(DeviceConfig(id="d1", link_id="L"))
        self.monitor.ensure_device(DeviceConfig(id="d2", link_id="L"))
        self.monitor.ensure_device(DeviceConfig(id="d3", link_id="L"))
        self.monitor.ensure_linkage(Linkage(id="pump", link_id="L"))
        settle(self.monitor, ["d1", "d2", "d3"])
        for t in (61, 71, 81):
            self.monitor.tick("L", at=t)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_state_survives_monitor_restart(self) -> None:
        token = self.monitor.snapshot()["links"][0]["state"]["fencing_token"]
        restarted, _ = build_monitor(self.tmp.name)
        link = restarted.snapshot()["links"][0]["state"]
        self.assertEqual(link["mode"], MODE_DEGRADED)
        self.assertEqual(link["fencing_token"], token)
        # gates read back from disk still show paused with no new commands
        gate = restarted.snapshot()["links"][0]["linkages"][0]
        self.assertEqual(gate["actual"], GATE_PAUSED)
        commands_before = len(restarted.commands("L"))
        restarted.tick("L", at=500)
        self.assertEqual(len(restarted.commands("L")), commands_before)

    def test_audit_and_snapshot_read_the_same_documents(self) -> None:
        events_on_disk = self.monitor.audit_trail(limit=1000)
        self.assertTrue(events_on_disk)
        kinds = {e["kind"] for e in events_on_disk}
        self.assertIn("device_offline", kinds)
        self.assertIn("linkage_pause_planned", kinds)
        store = Store.open(self.tmp.name)
        self.assertTrue(store.list("link-event"))
        self.assertTrue(store.list("link-device-state"))
        self.assertTrue(store.list("link-state"))

    def test_episode_opens_and_closes_once(self) -> None:
        seq = 5000
        for step in range(4):
            t = 90 + step * 10
            for device in ("d1", "d2", "d3"):
                seq += 1
                self.monitor.heartbeat(device, seq, at=t)
        # the outage episode (and only it) closes on this recovery
        episodes = self.monitor.episodes("L")
        outage = [episode for episode in episodes if "device" in episode["reason"]]
        self.assertEqual(len(outage), 1)
        self.assertEqual(outage[0]["status"], "closed")
        # a heartbeat-driven evaluation while still NORMAL changes no episodes
        before = len(episodes)
        seq += 1
        self.monitor.heartbeat("d1", seq, at=150)
        self.assertEqual(len(self.monitor.episodes("L")), before)


class ModelRoundTripCase(unittest.TestCase):
    def test_link_state_round_trips_new_fields(self) -> None:
        state = LinkState(id="L", stable_advanced_at=42)
        restored = LinkState.from_dict(state.as_dict())
        self.assertEqual(restored.stable_advanced_at, 42)

    def test_device_state_round_trips_drop_history(self) -> None:
        state = DeviceState(id="d", link_id="L", flap_at=[10, 20], last_drop_at=20, flapping=True)
        restored = DeviceState.from_dict(state.as_dict())
        self.assertEqual(restored.flap_at, [10, 20])
        self.assertEqual(restored.last_drop_at, 20)
        self.assertTrue(restored.flapping)


if __name__ == "__main__":
    unittest.main()
