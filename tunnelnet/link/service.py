"""Link online monitoring and degraded running.

Policy (fixed deliberately, see module README):

* A device loses one heartbeat -> ``JITTER`` (recorded and visible, but it does
  not trigger a control action). It loses ``miss_threshold`` heartbeats in a row
  -> ``OFFLINE``. Recovering needs ``recover_hits`` consecutive heartbeats, so a
  flapping link cannot bounce the controller.
* A link with at least ``offline_ratio`` of its devices offline enters
  ``DEGRADED`` and the ordinary linkages are paused (safety linkages stay
  armed). When every device is back the link moves to ``RECOVERING`` but stays
  paused until an operator confirms a stable window, then returns ``ONLINE``.

Every decision is persisted before a control action runs. Pause/resume run as a
compensatable saga: a failing controller rolls the persisted decision back.
Confirm and rollback are idempotent.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Protocol

from tunnelnet.store import Store

from . import persistence
from .gate import LinkageGate
from .model import (
    DEFAULT_INTERVAL,
    DEFAULT_MISS_THRESHOLD,
    DEFAULT_OFFLINE_RATIO,
    DEFAULT_RECOVER_HITS,
    DEFAULT_RECOVER_STABLE_SECONDS,
    Device,
    DeviceState,
    Link,
    LinkState,
    RecoveryResult,
    Transition,
)
from .report import LinkReport, LinkView, is_degraded, summarize_devices, system_mode


def monotonic() -> float:
    return time.monotonic()


class Clock:
    """A controllable monotonic clock, used to advance time deterministically.

    Production leaves ``value`` at ``None`` so every read returns the real
    monotonic clock; tests pin it to an explicit value.
    """

    def __init__(self, value: float | None = None) -> None:
        self._value = value

    def now(self) -> float:
        return monotonic() if self._value is None else self._value

    def set(self, value: float) -> None:
        self._value = value

    def advance(self, seconds: float) -> float:
        self._value = self.now() + seconds
        return self._value

    def __call__(self) -> float:
        return self.now()


class TransitionSink(Protocol):
    """Receives every state transition, e.g. the shared audit registry."""

    def write(self, transition: Transition) -> None:  # pragma: no cover - structural
        ...


class LinkMonitor:
    """Derives device/link state from heartbeats and owns degraded episodes."""

    def __init__(
        self,
        store: Store,
        gate: LinkageGate | None = None,
        clock: Callable[[], float] = monotonic,
        sink: TransitionSink | None = None,
    ) -> None:
        self._store = store
        self._gate = gate or LinkageGate()
        self._clock = clock
        self._sink = sink
        self._lock = threading.RLock()
        self._links: dict[str, Link] = {link.id: link for link in persistence.list_links(store)}
        self._devices: dict[str, Device] = {
            device.id: device for device in persistence.list_devices(store)
        }
        events = persistence.list_events(store)
        self._seq = events[-1].seq if events else 0
        # On restart a degraded link must still have its linkages paused.
        if any(link.state is not LinkState.ONLINE for link in self._links.values()):
            self._gate.pause_normal()

    # ------------------------------------------------------------------ setup

    def register_link(
        self,
        link_id: str,
        name: str = "",
        interval: float = DEFAULT_INTERVAL,
        miss_threshold: int = DEFAULT_MISS_THRESHOLD,
        recover_hits: int = DEFAULT_RECOVER_HITS,
        offline_ratio: float = DEFAULT_OFFLINE_RATIO,
        recover_stable_seconds: float = DEFAULT_RECOVER_STABLE_SECONDS,
    ) -> Link:
        with self._lock:
            existing = self._links.get(link_id)
            if existing is not None:
                return existing
            if not link_id:
                raise ValueError("link id is required")
            link = Link(
                id=link_id,
                name=name or link_id,
                interval=interval,
                miss_threshold=max(1, miss_threshold),
                recover_hits=max(1, recover_hits),
                offline_ratio=offline_ratio,
                recover_stable_seconds=recover_stable_seconds,
            )
            self._links[link_id] = link
            persistence.save_link(self._store, link)
            return link

    def register_device(self, device_id: str, link_id: str) -> Device:
        with self._lock:
            existing = self._devices.get(device_id)
            if existing is not None:
                return existing
            if not device_id:
                raise ValueError("device id is required")
            if link_id not in self._links:
                raise ValueError(f"link {link_id} not found")
            device = Device(id=device_id, link_id=link_id, registered_at=self._clock())
            self._devices[device_id] = device
            persistence.save_device(self._store, device)
            return device

    # -------------------------------------------------------------- heartbeats

    def heartbeat(self, device_id: str, at: float | None = None) -> Device:
        """Record one heartbeat and re-evaluate the owning link."""

        with self._lock:
            device = self._devices.get(device_id)
            if device is None:
                raise ValueError(f"device {device_id} not registered")
            link = self._links[device.link_id]
            stamp = self._clock() if at is None else at
            previous = device.state

            device.last_seen = stamp
            device.missed = 0
            if previous is DeviceState.UNKNOWN:
                device.recover_hits = 0
                self._set_device_state(device, DeviceState.ONLINE, link)
            elif previous is DeviceState.OFFLINE:
                device.recover_hits += 1
                if device.recover_hits >= link.recover_hits:
                    device.recover_hits = 0
                    self._set_device_state(device, DeviceState.ONLINE, link)
            elif previous is DeviceState.JITTER:
                device.recover_hits = 0
                self._set_device_state(device, DeviceState.ONLINE, link)
            persistence.save_device(self._store, device)
            self._evaluate_link(link, stamp)
            return device

    def sweep(self, at: float | None = None) -> list[Transition]:
        """Age every seen device by the clock and evaluate every link once."""

        with self._lock:
            stamp = self._clock() if at is None else at
            before = self._seq
            for device in self._devices.values():
                self._age_device(device, stamp)
            for link in list(self._links.values()):
                self._evaluate_link(link, stamp)
            return [event for event in persistence.list_events(self._store) if event.seq > before]

    def _age_device(self, device: Device, now: float) -> None:
        if device.last_seen is None:
            return
        link = self._links[device.link_id]
        elapsed = max(0.0, now - device.last_seen)
        missed = int(elapsed // link.interval)
        missed = min(missed, link.miss_threshold)
        if missed != device.missed:
            device.missed = missed
        if missed >= link.miss_threshold:
            target = DeviceState.OFFLINE
            device.recover_hits = 0
        elif missed >= 1:
            target = DeviceState.JITTER
            device.recover_hits = 0
        else:
            target = DeviceState.ONLINE
        if target is not device.state:
            self._set_device_state(device, target, link)
        persistence.save_device(self._store, device)

    def _set_device_state(self, device: Device, new: DeviceState, link: Link) -> None:
        old = device.state
        if old is new:
            return
        if old is DeviceState.OFFLINE or new is DeviceState.OFFLINE:
            device.flaps += 1
        device.state = new
        persistence.save_device(self._store, device)
        self._emit(
            Transition(
                scope="device",
                target_id=device.id,
                old=old.value,
                new=new.value,
                at=self._clock(),
                episode=link.episode,
            )
        )

    # --------------------------------------------------------- link aggregation

    def _evaluate_link(self, link: Link, now: float) -> None:
        devices = [d for d in self._devices.values() if d.link_id == link.id]
        if not devices:
            return
        offline = sum(1 for d in devices if d.state is DeviceState.OFFLINE)
        online = sum(1 for d in devices if d.state is DeviceState.ONLINE)
        ratio = offline / len(devices)
        degraded_trigger = ratio >= link.offline_ratio and offline > 0
        healthy = offline == 0 and online > 0

        if link.state is LinkState.ONLINE and degraded_trigger:
            self._enter_degraded(link, now, "offline ratio reached")
        elif link.state is LinkState.DEGRADED and healthy:
            self._enter_recovering(link, now)
        elif link.state is LinkState.RECOVERING and degraded_trigger:
            self._enter_degraded(link, now, "relapse during recovery")

    def _enter_degraded(self, link: Link, now: float, reason: str) -> None:
        if link.state is LinkState.DEGRADED:
            return
        snapshot = (link.state, link.degraded_since, link.recovering_since, link.episode)
        link.state = LinkState.DEGRADED
        link.degraded_since = now
        link.recovering_since = None
        link.episode += 1
        persistence.save_link(self._store, link)
        try:
            self._gate.pause_normal()
        except Exception:
            self._compensate(self._gate.resume_normal)
            link.state, link.degraded_since, link.recovering_since, link.episode = snapshot
            persistence.save_link(self._store, link)
            raise
        self._emit(
            Transition(
                scope="link",
                target_id=link.id,
                old=snapshot[0].value,
                new=LinkState.DEGRADED.value,
                at=now,
                episode=link.episode,
                reason=reason,
            )
        )

    def _enter_recovering(self, link: Link, now: float) -> None:
        if link.state is LinkState.RECOVERING:
            return
        old = link.state
        link.state = LinkState.RECOVERING
        link.recovering_since = now
        persistence.save_link(self._store, link)
        # Linkages deliberately stay paused: recovery is not confirmed yet.
        self._emit(
            Transition(
                scope="link",
                target_id=link.id,
                old=old.value,
                new=LinkState.RECOVERING.value,
                at=now,
                episode=link.episode,
                reason="devices back, awaiting stable confirmation",
            )
        )

    # --------------------------------------------------------------- recovery

    def confirm_recovery(self, link_id: str, force: bool = False) -> RecoveryResult:
        """Resume paused linkages and return a recovering link to normal."""

        with self._lock:
            link = self._require_link(link_id)
            now = self._clock()

            if link.state is LinkState.ONLINE:
                return RecoveryResult(link_id, LinkState.ONLINE, link.episode, (), False)
            if link.state is LinkState.DEGRADED:
                raise ValueError("link is still degraded")

            devices = [d for d in self._devices.values() if d.link_id == link.id]
            if any(d.state is DeviceState.OFFLINE for d in devices):
                raise ValueError("devices are still offline")
            if not force and link.recovering_since is not None:
                stable_for = now - link.recovering_since
                if stable_for < link.recover_stable_seconds:
                    raise ValueError(
                        f"stable window not elapsed ({stable_for:.0f}s"
                        f" < {link.recover_stable_seconds:.0f}s)"
                    )

            snapshot = (link.state, link.degraded_since, link.recovering_since, link.episode)
            link.state = LinkState.ONLINE
            link.degraded_since = None
            link.recovering_since = None
            persistence.save_link(self._store, link)
            try:
                restored = tuple(self._gate.resume_normal())
            except Exception:
                self._compensate(self._gate.pause_normal)
                link.state, link.degraded_since, link.recovering_since, link.episode = snapshot
                persistence.save_link(self._store, link)
                raise
            self._emit(
                Transition(
                    scope="link",
                    target_id=link.id,
                    old=snapshot[0].value,
                    new=LinkState.ONLINE.value,
                    at=now,
                    episode=link.episode,
                    reason="manual confirm" if not force else "forced confirm",
                )
            )
            return RecoveryResult(link_id, LinkState.ONLINE, link.episode, restored, False)

    def rollback_recovery(self, link_id: str) -> RecoveryResult:
        """Undo a confirm (or a pending recovery) by re-entering degraded mode.

        Idempotent: rolling back a link that is already degraded changes nothing
        and reports ``rolled_back=False``.
        """

        with self._lock:
            link = self._require_link(link_id)
            if link.state is LinkState.DEGRADED:
                return RecoveryResult(link_id, LinkState.DEGRADED, link.episode, (), False)

            now = self._clock()
            old = link.state
            snapshot = (link.state, link.degraded_since, link.recovering_since, link.episode)
            link.state = LinkState.DEGRADED
            link.degraded_since = now
            link.recovering_since = None
            link.episode += 1
            persistence.save_link(self._store, link)
            try:
                self._gate.pause_normal()
            except Exception:
                self._compensate(self._gate.resume_normal)
                link.state, link.degraded_since, link.recovering_since, link.episode = snapshot
                persistence.save_link(self._store, link)
                raise
            self._emit(
                Transition(
                    scope="link",
                    target_id=link.id,
                    old=old.value,
                    new=LinkState.DEGRADED.value,
                    at=now,
                    episode=link.episode,
                    reason="manual rollback",
                )
            )
            return RecoveryResult(link_id, LinkState.DEGRADED, link.episode, (), True)

    # ------------------------------------------------------------------ audit

    def _compensate(self, reverse: Callable[[], list[str]]) -> None:
        """Best effort reverse action; a compensation failure must not mask the
        original saga error, so it is swallowed after undoing what it can."""

        try:
            reverse()
        except Exception:
            pass

    def _emit(self, transition: Transition) -> None:
        self._seq += 1
        stamped = Transition(
            scope=transition.scope,
            target_id=transition.target_id,
            old=transition.old,
            new=transition.new,
            at=transition.at,
            episode=transition.episode,
            reason=transition.reason,
            seq=self._seq,
        )
        persistence.save_event(self._store, stamped)
        if self._sink is not None:
            self._sink.write(stamped)

    def events(self) -> list[Transition]:
        with self._lock:
            return persistence.list_events(self._store)

    # ------------------------------------------------------------------- views

    def _require_link(self, link_id: str) -> Link:
        link = self._links.get(link_id)
        if link is None:
            raise ValueError(f"link {link_id} not found")
        return link

    def find_link(self, link_id: str) -> Link | None:
        with self._lock:
            return self._links.get(link_id)

    def find_device(self, device_id: str) -> Device | None:
        with self._lock:
            return self._devices.get(device_id)

    def links(self) -> list[Link]:
        with self._lock:
            return [self._links[key] for key in sorted(self._links)]

    def devices(self, link_id: str | None = None) -> list[Device]:
        with self._lock:
            values = list(self._devices.values())
            if link_id is not None:
                values = [d for d in values if d.link_id == link_id]
            return sorted(values, key=lambda d: d.id)

    def mode(self) -> str:
        return system_mode(self.links())

    def degraded(self) -> bool:
        return is_degraded(self.links())

    def allowed(self, key: str) -> bool:
        return self._gate.is_allowed(key)

    def gate(self) -> LinkageGate:
        return self._gate

    def link_view(self, link_id: str) -> LinkView:
        with self._lock:
            link = self._require_link(link_id)
            devices = self.devices(link_id)
            return LinkView(link=link, devices=tuple(devices), summary=summarize_devices(devices))

    def report(self) -> LinkReport:
        with self._lock:
            links = self.links()
            views = [self.link_view(link.id) for link in links]
            devices = summarize_devices(self.devices())
            return LinkReport(
                mode=system_mode(links),
                links=tuple(views),
                devices=devices,
                paused=tuple(self._gate.paused_linkages()),
            )

    def describe(self) -> str:
        links = self.links()
        degraded = sum(1 for link in links if link.state is not LinkState.ONLINE)
        return f"links={len(links)} degraded={degraded} devices={len(self._devices)}"
