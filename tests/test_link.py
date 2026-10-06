"""Behavioural tests for link online monitoring and degraded running."""

from __future__ import annotations

import tempfile
import unittest

from tunnelnet.link import (
    DeviceState,
    LinkageGate,
    LinkMonitor,
    LinkState,
    summarize_devices,
    system_mode,
)
from tunnelnet.store import Store


class Clock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def pause(self, key: str) -> None:
        self.calls.append(("pause", key))

    def resume(self, key: str) -> None:
        self.calls.append(("resume", key))


def build_monitor(tmp: str, clock: Clock, recorder: Recorder | None = None) -> LinkMonitor:
    store = Store.open(tmp)
    gate = LinkageGate(controller=recorder) if recorder is not None else LinkageGate()
    monitor = LinkMonitor(store, gate=gate, clock=clock)
    monitor.register_link(
        "L1",
        interval=10.0,
        miss_threshold=3,
        recover_hits=2,
        offline_ratio=0.5,
        recover_stable_seconds=30.0,
    )
    monitor.register_device("d1", "L1")
    monitor.register_device("d2", "L1")
    monitor.heartbeat("d1", 0.0)
    monitor.heartbeat("d2", 0.0)
    return monitor


class HeartbeatPolicyCase(unittest.TestCase):
    def test_single_miss_is_jitter_not_offline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock()
            monitor = build_monitor(tmp, clock)
            clock.value = 12.0
            monitor.sweep(12.0)
            self.assertIs(monitor.find_device("d1").state, DeviceState.JITTER)
            self.assertEqual(monitor.mode(), LinkState.ONLINE.value)
            # Jitter alone does not pause a linkage.
            self.assertTrue(monitor.allowed("access.release"))

    def test_three_consecutive_misses_is_offline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock()
            monitor = build_monitor(tmp, clock)
            clock.value = 32.0
            monitor.sweep(32.0)
            self.assertIs(monitor.find_device("d1").state, DeviceState.OFFLINE)
            self.assertEqual(monitor.mode(), LinkState.DEGRADED.value)

    def test_unknown_device_does_not_degrade_link(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock()
            store = Store.open(tmp)
            monitor = LinkMonitor(store, clock=clock)
            monitor.register_link("L1")
            monitor.register_device("never", "L1")
            monitor.sweep(5.0)
            self.assertEqual(monitor.mode(), LinkState.ONLINE.value)
            self.assertIs(monitor.find_device("never").state, DeviceState.UNKNOWN)

    def test_recovery_needs_two_consecutive_heartbeats(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock()
            monitor = build_monitor(tmp, clock)
            clock.value = 40.0
            monitor.sweep(40.0)
            self.assertEqual(monitor.mode(), "degraded")
            monitor.heartbeat("d1", 41.0)
            self.assertIs(monitor.find_device("d1").state, DeviceState.OFFLINE)
            monitor.heartbeat("d1", 42.0)
            self.assertIs(monitor.find_device("d1").state, DeviceState.ONLINE)

    def test_flap_counter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock()
            monitor = build_monitor(tmp, clock)
            clock.value = 40.0
            monitor.sweep(40.0)
            monitor.heartbeat("d1", 41.0)
            monitor.heartbeat("d1", 42.0)
            self.assertEqual(monitor.find_device("d1").flaps, 2)

    def test_register_unknown_device_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            monitor = LinkMonitor(Store.open(tmp))
            with self.assertRaises(ValueError):
                monitor.heartbeat("ghost")


class DegradationAndRecoveryCase(unittest.TestCase):
    def test_degrade_pauses_normal_but_keeps_safety_armed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock()
            monitor = build_monitor(tmp, clock)
            clock.value = 40.0
            monitor.sweep(40.0)
            self.assertFalse(monitor.allowed("access.release"))
            self.assertFalse(monitor.allowed("vent.run"))
            self.assertTrue(monitor.allowed("fire.suppression"))
            self.assertTrue(monitor.allowed("audit.record"))
            paused = {item.key for item in monitor.gate().paused_linkages()}
            self.assertNotIn("fire.suppression", paused)
            self.assertIn("access.release", paused)

    def test_half_offline_triggers_whole_link_degraded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock()
            monitor = build_monitor(tmp, clock)
            # d1 stays at t=0 (stale), d2 keeps beating to t=40 (fresh): 1/2 offline.
            monitor.heartbeat("d2", 40.0)
            monitor._age_device(monitor.find_device("d1"), 40.0)
            monitor._evaluate_link(monitor.find_link("L1"), 40.0)
            self.assertIs(monitor.find_device("d1").state, DeviceState.OFFLINE)
            self.assertIs(monitor.find_device("d2").state, DeviceState.ONLINE)
            self.assertEqual(monitor.mode(), "degraded")

    def test_recovery_is_manual_after_stable_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock()
            monitor = build_monitor(tmp, clock)
            clock.value = 40.0
            monitor.sweep(40.0)
            monitor.heartbeat("d1", 41.0)
            monitor.heartbeat("d1", 42.0)
            monitor.heartbeat("d2", 41.0)
            monitor.heartbeat("d2", 42.0)
            self.assertEqual(monitor.find_link("L1").state, LinkState.RECOVERING)
            self.assertFalse(monitor.allowed("access.release"))

            with self.assertRaises(ValueError):
                monitor.confirm_recovery("L1")
            clock.value = 75.0
            result = monitor.confirm_recovery("L1")
            self.assertIs(result.state, LinkState.ONLINE)
            self.assertTrue(monitor.allowed("access.release"))

    def test_confirm_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock()
            monitor = build_monitor(tmp, clock)
            clock.value = 40.0
            monitor.sweep(40.0)
            monitor.heartbeat("d1", 41.0)
            monitor.heartbeat("d1", 72.0)
            monitor.heartbeat("d2", 41.0)
            monitor.heartbeat("d2", 72.0)
            clock.value = 105.0
            first = monitor.confirm_recovery("L1")
            second = monitor.confirm_recovery("L1")
            self.assertTrue(first.restored)
            self.assertEqual(second.restored, ())
            self.assertEqual(second.episode, first.episode)

    def test_confirm_blocked_when_devices_offline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock()
            monitor = build_monitor(tmp, clock)
            clock.value = 40.0
            monitor.sweep(40.0)
            # Only one device returns; link cannot reach recovering without both.
            monitor.heartbeat("d1", 41.0)
            monitor.heartbeat("d1", 42.0)
            self.assertEqual(monitor.find_link("L1").state, LinkState.DEGRADED)
            with self.assertRaises(ValueError):
                monitor.confirm_recovery("L1")

    def test_rollback_re_enters_degraded_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock()
            monitor = build_monitor(tmp, clock)
            clock.value = 40.0
            monitor.sweep(40.0)
            monitor.heartbeat("d1", 71.0)
            monitor.heartbeat("d1", 72.0)
            monitor.heartbeat("d2", 71.0)
            monitor.heartbeat("d2", 72.0)
            clock.value = 105.0
            monitor.confirm_recovery("L1")
            self.assertTrue(monitor.allowed("access.release"))

            rolled = monitor.rollback_recovery("L1")
            self.assertTrue(rolled.rolled_back)
            self.assertIs(rolled.state, LinkState.DEGRADED)
            self.assertFalse(monitor.allowed("access.release"))

            again = monitor.rollback_recovery("L1")
            self.assertFalse(again.rolled_back)
            self.assertIs(again.state, LinkState.DEGRADED)

    def test_episode_increments_per_degraded_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock()
            monitor = build_monitor(tmp, clock)
            clock.value = 40.0
            monitor.sweep(40.0)
            self.assertEqual(monitor.find_link("L1").episode, 1)
            monitor.heartbeat("d1", 71.0)
            monitor.heartbeat("d1", 72.0)
            monitor.heartbeat("d2", 71.0)
            monitor.heartbeat("d2", 72.0)
            clock.value = 105.0
            monitor.confirm_recovery("L1")
            monitor.rollback_recovery("L1")
            self.assertEqual(monitor.find_link("L1").episode, 2)


class SagaAndPersistenceCase(unittest.TestCase):
    def test_pause_failure_rolls_persisted_decision_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock()

            class FailFirst:
                def pause(self, key: str) -> None:
                    if key == "access.release":
                        raise RuntimeError("controller down")

                def resume(self, key: str) -> None:
                    return None

            store = Store.open(tmp)
            monitor = LinkMonitor(store, gate=LinkageGate(controller=FailFirst()), clock=clock)
            monitor.register_link("L1", interval=10, miss_threshold=3, offline_ratio=0.5)
            monitor.register_device("d1", "L1")
            monitor.register_device("d2", "L1")
            monitor.heartbeat("d1", 0.0)
            monitor.heartbeat("d2", 0.0)
            clock.value = 40.0
            with self.assertRaises(RuntimeError):
                monitor.sweep(40.0)
            self.assertEqual(monitor.find_link("L1").state, LinkState.ONLINE)
            self.assertEqual([e for e in monitor.events() if e.scope == "link"], [])

    def test_partial_pause_failure_compensates_paused_linkages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock()

            class FailLate:
                def pause(self, key: str) -> None:
                    if key == "energy.meter":  # after access/env/vent/patrol paused
                        raise RuntimeError("late failure")

                def resume(self, key: str) -> None:
                    return None

            store = Store.open(tmp)
            gate = LinkageGate(controller=FailLate())
            monitor = LinkMonitor(store, gate=gate, clock=clock)
            monitor.register_link("L1", interval=10, miss_threshold=3, offline_ratio=0.5)
            monitor.register_device("d1", "L1")
            monitor.register_device("d2", "L1")
            monitor.heartbeat("d1", 0.0)
            monitor.heartbeat("d2", 0.0)
            clock.value = 40.0
            with self.assertRaises(RuntimeError):
                monitor.sweep(40.0)
            self.assertEqual(gate.paused_linkages(), [])

    def test_state_survives_restart_and_repauses_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock()
            monitor = build_monitor(tmp, clock)
            clock.value = 40.0
            monitor.sweep(40.0)

            restarted = LinkMonitor(Store.open(tmp), gate=LinkageGate(), clock=clock)
            self.assertEqual(restarted.mode(), "degraded")
            self.assertFalse(restarted.allowed("access.release"))
            self.assertEqual(
                [device.state for device in restarted.devices()],
                [DeviceState.OFFLINE, DeviceState.OFFLINE],
            )

    def test_events_durable_and_ordered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock()
            store = Store.open(tmp)
            monitor = build_monitor(tmp, clock)
            clock.value = 40.0
            monitor.sweep(40.0)
            reloaded = LinkMonitor(store, clock=clock)
            events = reloaded.events()
            self.assertTrue(events)
            self.assertEqual([e.seq for e in events], sorted(e.seq for e in events))
            self.assertIn("link_degraded", [e.event() for e in events])


class ReportCase(unittest.TestCase):
    def test_summary_and_mode_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock()
            monitor = build_monitor(tmp, clock)
            summary = summarize_devices(monitor.devices())
            self.assertEqual((summary.total, summary.online), (2, 2))
            self.assertEqual(system_mode(monitor.links()), "online")
            report = monitor.report()
            self.assertEqual(report.mode, "online")
            self.assertEqual(report.paused, ())


if __name__ == "__main__":
    unittest.main()
