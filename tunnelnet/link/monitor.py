"""Orchestrates device heartbeats, link modes and linkage commands.

Timing contract (why actions never precede the verdict):

1. evaluate every device from persisted state;
2. persist new device states and their events;
3. decide link availability and the next run mode, persist that transition;
4. reconcile the desired gate of each linkage and append at-most-once
   commands to the outbox;
5. only then deliver commands to actuators, stamping each delivery with the
   link's fencing token;
6. persist command outcomes and roll the mode back when a resume fails.

Every step above is durable before the next one begins, so a crash leaves a
record that describes exactly what was intended; re-running a tick repeats
intents that never completed and no-ops the ones that did.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from typing import Protocol

from .model import (
    ACTION_PAUSE,
    ACTION_RESUME,
    CMD_ACKED,
    CMD_FAILED,
    CMD_PENDING,
    CMD_STALE,
    EPISODE_CLOSED,
    EPISODE_OPEN,
    GATE_ACTIVE,
    GATE_PAUSED,
    GATE_PAUSE_FAILED,
    GATE_RESUME_FAILED,
    LINK_DOWN,
    LINK_UP,
    MODE_DEGRADED,
    MODE_NORMAL,
    MODE_RECOVERING,
    DeviceConfig,
    DeviceState,
    Episode,
    LinkCommand,
    LinkConfig,
    LinkEvent,
    LinkState,
    Linkage,
    LinkageState,
)
from .policy import link_availability
from .repository import LinkRepository

ENTITY_DEVICE = "device"
ENTITY_LINK = "link"
ENTITY_LINKAGE = "linkage"
ENTITY_EPISODE = "episode"
ENTITY_COMMAND = "command"

EV_LINK_UP = "link_up"
EV_LINK_DOWN = "link_down"
EV_MODE_DEGRADED = "mode_degraded"
EV_MODE_RECOVERING = "mode_recovering"
EV_MODE_NORMAL = "mode_normal"
EV_ROLLBACK = "mode_rollback"
EV_HOLD = "mode_hold"
EV_HOLD_CLEARED = "mode_hold_cleared"
EV_PAUSE_PLANNED = "linkage_pause_planned"
EV_RESUME_PLANNED = "linkage_resume_planned"
EV_PAUSE_FAILED = "linkage_pause_failed"
EV_RESUME_FAILED = "linkage_resume_failed"
EV_EPISODE_OPENED = "episode_opened"
EV_EPISODE_CLOSED = "episode_closed"


def monotonic_seconds() -> int:
    """Default integer clock, seconds since the epoch."""

    return int(time.time())


class LinkActuator(Protocol):
    """Field-side sink for pause/resume commands.

    Implementations must reject a command whose ``token`` is older than the
    newest token they have accepted for that link - that is the fencing rule
    that makes a rolled-back recovery unable to re-enable a linkage.
    """

    def pause(self, link_id: str, linkage_id: str, token: int) -> None: ...

    def resume(self, link_id: str, linkage_id: str, token: int) -> None: ...


class RecordingActuator:
    """In-process actuator used by the console, demos and tests."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tokens: dict[str, int] = {}
        self.paused: set[str] = set()
        self.calls: list[tuple[str, str, int]] = []
        self.fail_on: tuple[str, str] | None = None

    def _check_fence(self, link_id: str, token: int) -> None:
        accepted = self._tokens.get(link_id, -1)
        if token < accepted:
            raise StaleCommand(link_id, token, accepted)
        self._tokens[link_id] = token

    def pause(self, link_id: str, linkage_id: str, token: int) -> None:
        with self._lock:
            self._check_fence(link_id, token)
            self.calls.append((ACTION_PAUSE, linkage_id, token))
            if self.fail_on == (ACTION_PAUSE, linkage_id):
                raise ActuatorFailure(f"pause rejected for {linkage_id}")
            self.paused.add(linkage_id)

    def resume(self, link_id: str, linkage_id: str, token: int) -> None:
        with self._lock:
            self._check_fence(link_id, token)
            self.calls.append((ACTION_RESUME, linkage_id, token))
            if self.fail_on == (ACTION_RESUME, linkage_id):
                raise ActuatorFailure(f"resume rejected for {linkage_id}")
            self.paused.discard(linkage_id)

    def is_paused(self, linkage_id: str) -> bool:
        with self._lock:
            return linkage_id in self.paused

    def accepted_token(self, link_id: str) -> int:
        with self._lock:
            return self._tokens.get(link_id, -1)

    def fail_next(self, action: str, linkage_id: str) -> None:
        self.fail_on = (action, linkage_id)

    def clear_faults(self) -> None:
        self.fail_on = None


class StaleCommand(RuntimeError):
    def __init__(self, link_id: str, token: int, accepted: int) -> None:
        super().__init__(f"stale token {token} < {accepted} on link {link_id}")
        self.link_id = link_id
        self.token = token
        self.accepted = accepted


class ActuatorFailure(RuntimeError):
    pass


class LinkMonitor:
    """Heartbeat ingestion, mode state machine and linkage reconciliation."""

    def __init__(
        self,
        repo: LinkRepository,
        actuator: LinkActuator | None = None,
        clock: Callable[[], int] = monotonic_seconds,
    ) -> None:
        self._repo = repo
        self._actuator: LinkActuator = actuator or RecordingActuator()
        self._clock = clock
        self._lock = threading.RLock()
        # Events emitted during an explicit evaluation share that evaluation's
        # timestamp so device verdicts, mode transitions and command records
        # are ordered on one time base (deterministic in tests with a fake at).
        self._eval_at: int | None = None

    # -- registration / bootstrap ------------------------------------------

    def ensure_link(self, config: LinkConfig) -> LinkState:
        """Create the persisted state for a link on first sight."""

        with self._lock:
            self._repo.save_link_config(config)
            state = self._repo.load_link_state(config.id)
            if state is not None:
                return state
            state = LinkState(id=config.id, since=self._clock())
            self._repo.save_link_state(state)
            episode = self._new_episode(config.id, "bootstrap: link unproven at startup")
            self._repo.save_episode(episode)
            self._event(EV_EPISODE_OPENED, ENTITY_EPISODE, episode.id, episode.reason, {"link_id": config.id})
            self._event(EV_LINK_DOWN, ENTITY_LINK, config.id, state.reason)
            self._event(EV_MODE_DEGRADED, ENTITY_LINK, config.id, state.reason)
            return state

    def ensure_device(self, device: DeviceConfig) -> DeviceState:
        self.ensure_link_config_only(LinkConfig(id=device.link_id))
        with self._lock:
            self._repo.save_device_config(device)
            state = self._repo.load_device_state(device.id)
            if state is None:
                state = DeviceState(id=device.id, link_id=device.link_id, entered_at=self._clock())
                self._repo.save_device_state(state)
            return state

    def ensure_link_config_only(self, fallback: LinkConfig) -> LinkConfig:
        with self._lock:
            config = self._repo.load_link_config(fallback.id)
            if config is None:
                config = fallback
                self._repo.save_link_config(config)
            return config

    def ensure_linkage(self, linkage: Linkage) -> LinkageState:
        with self._lock:
            self.ensure_link_config_only(LinkConfig(id=linkage.link_id))
            self._repo.save_linkage(linkage)
            state = self._repo.load_linkage_state(linkage.id)
            if state is None:
                state = LinkageState(id=linkage.id, link_id=linkage.link_id, updated_at=self._clock())
                self._repo.save_linkage_state(state)
            return state

    # -- heartbeats ---------------------------------------------------------

    def heartbeat(self, device_id: str, seq: int, at: int | None = None) -> DeviceState:
        """Apply one inbound heartbeat and run the affected link's tick."""

        with self._lock:
            config = self._require_config_for_device(device_id)
            now = at if at is not None else self._clock()
            state = self._require_device_state(device_id)
            with self._timed(now):
                next_state, events = _evaluate(
                    state, config, now, heartbeat_seq=seq
                )
                if next_state is not state:
                    self._repo.save_device_state(next_state)
                    self._persist_device_events(device_id, events)
                self._evaluate_link(config.id, now)
            return next_state

    def tick(self, link_id: str, at: int | None = None) -> LinkState:
        """Re-evaluate every device on a link from wall-clock timeouts."""

        with self._lock:
            now = at if at is not None else self._clock()
            config = self._require_link_config(link_id)
            with self._timed(now):
                for device in self._repo.list_device_configs(link_id):
                    state = self._require_device_state(device.id)
                    next_state, events = _evaluate(state, config, now)
                    if next_state is not state:
                        self._repo.save_device_state(next_state)
                        self._persist_device_events(device.id, events)
                return self._evaluate_link(link_id, now)

    @contextmanager
    def _timed(self, now: int):
        previous = self._eval_at
        self._eval_at = now
        try:
            yield
        finally:
            self._eval_at = previous

    def tick_all(self, at: int | None = None) -> list[LinkState]:
        with self._lock:
            now = at if at is not None else self._clock()
            return [self.tick(config.id, now) for config in self._repo.list_link_configs()]

    # -- link mode state machine -------------------------------------------

    def _evaluate_link(self, link_id: str, now: int) -> LinkState:
        config = self._require_link_config(link_id)
        link = self._require_link_state(link_id)
        pairs = [(device, self._require_device_state(device.id)) for device in self._repo.list_device_configs(link_id)]
        availability, unavailable, ratio, critical_lost = link_availability(config, pairs)
        reason = self._down_reason(unavailable, ratio, critical_lost)
        available = availability == LINK_UP

        link.offline_devices = unavailable
        link.unavailable_ratio = round(ratio, 4)
        was_up = link.availability == LINK_UP

        if not available:
            link.stable_count = 0
            link.stable_advanced_at = None
            link.availability = LINK_DOWN
            link.reason = reason
            if was_up:
                self._event(
                    EV_LINK_DOWN,
                    ENTITY_LINK,
                    link_id,
                    reason,
                    {"unavailable": unavailable, "ratio": round(ratio, 4), "critical": critical_lost},
                )
            self._go_degraded(link, now, reason)
        else:
            if not was_up:
                self._event(
                    EV_LINK_UP,
                    ENTITY_LINK,
                    link_id,
                    f"link healthy; unavailable={unavailable}",
                    {"unavailable": unavailable, "ratio": round(ratio, 4)},
                )
            link.availability = LINK_UP
            link.reason = f"healthy; suspect/unavailable devices={unavailable}"
            self._advance_recovery(link, config, now)

        link.version += 1
        link.updated_at = now
        self._repo.save_link_state(link)
        self._reconcile_linkages(link, now)
        return self._require_link_state(link_id)

    def _down_reason(self, unavailable: list[str], ratio: float, critical_lost: list[str]) -> str:
        if critical_lost:
            return f"critical devices lost: {', '.join(critical_lost)}"
        return f"{len(unavailable)} device(s) unavailable (ratio {ratio:.2f})"

    def _go_degraded(self, link: LinkState, now: int, reason: str) -> None:
        link.availability = LINK_DOWN
        link.stable_count = 0
        link.stable_advanced_at = None

        if link.mode == MODE_RECOVERING:
            # Recovery did not survive: roll every intent back to paused and
            # bump the fencing epoch so in-flight resume commands are stale.
            link.mode = MODE_DEGRADED
            link.fencing_token += 1
            link.since = now
            link.reason = f"rolled back to degraded: {reason}"
            self._event(EV_ROLLBACK, ENTITY_LINK, link.id, link.reason, {"token": link.fencing_token})
            return

        if link.mode != MODE_DEGRADED:
            link.mode = MODE_DEGRADED
            link.fencing_token += 1
            link.since = now
            self._event(EV_MODE_DEGRADED, ENTITY_LINK, link.id, reason, {"token": link.fencing_token})
            episode = self._new_episode(link.id, reason)
            self._repo.save_episode(episode)
            self._event(EV_EPISODE_OPENED, ENTITY_EPISODE, episode.id, reason, {"link_id": link.id})
        else:
            link.reason = reason

    def _advance_recovery(self, link: LinkState, config: LinkConfig, now: int) -> None:
        if link.mode == MODE_NORMAL:
            return
        if link.hold:
            link.stable_count = 0
            link.stable_advanced_at = None
            link.mode = MODE_DEGRADED
            link.reason = "link healthy but operator hold is set; staying degraded"
            return
        # A link that has never proven a single device healthy cannot start
        # recovering: availability is computed from "nothing is offline yet",
        # not from positive evidence.
        proven = [
            state
            for device in self._repo.list_device_configs(link.id)
            if (state := self._repo.load_device_state(device.id)) is not None
            and state.last_seq >= 0
        ]
        if not proven:
            link.mode = MODE_DEGRADED
            link.reason = "awaiting first heartbeats before leaving degraded mode"
            return
        # Heartbeat ingestion and the periodic scheduler may both evaluate the
        # same second: advance the stability counter at most once per second.
        if link.stable_advanced_at != now:
            link.stable_count += 1
            link.stable_advanced_at = now
        if link.mode == MODE_DEGRADED:
            link.mode = MODE_RECOVERING
            link.since = now
            link.reason = "link healthy; verifying stability before resuming linkages"
            self._event(
                EV_MODE_RECOVERING,
                ENTITY_LINK,
                link.id,
                link.reason,
                {"stable": link.stable_count, "required": config.stable_ticks},
            )
            return
        # RECOVERING: wait out the stable window.
        link.reason = f"recovery verification {link.stable_count}/{config.stable_ticks}"
        if link.stable_count >= config.stable_ticks:
            link.mode = MODE_NORMAL
            link.since = now
            link.reason = "link healthy and stable; linkages resumed"
            self._event(EV_MODE_NORMAL, ENTITY_LINK, link.id, link.reason, {"token": link.fencing_token})

    # -- linkage reconciliation / outbox -----------------------------------

    def _reconcile_linkages(self, link: LinkState, now: int) -> None:
        rolled_back = False
        for linkage in self._repo.list_linkages(link.id):
            gate = self._repo.load_linkage_state(linkage.id)
            if gate is None:
                gate = LinkageState(id=linkage.id, link_id=link.id, updated_at=now)
            # Fail-safe linkages stay active no matter what; every ordinary
            # linkage follows the run mode.
            desired = GATE_ACTIVE if linkage.fail_safe or link.mode == MODE_NORMAL else GATE_PAUSED
            if desired != gate.desired:
                gate.desired = desired
                self._event(
                    EV_PAUSE_PLANNED if desired == GATE_PAUSED else EV_RESUME_PLANNED,
                    ENTITY_LINKAGE,
                    linkage.id,
                    f"desired gate -> {desired} ({'fail-safe' if linkage.fail_safe else link.mode})",
                    {"desired": desired, "mode": link.mode, "fail_safe": linkage.fail_safe},
                )
            rolled_back = self._reconcile_gate(link, linkage, gate, now) or rolled_back

        # A failed resume rolled the mode back mid-loop: run one more pass so
        # every linkage is driven toward the new PAUSED intent straight away.
        if rolled_back:
            for linkage in self._repo.list_linkages(link.id):
                gate = self._repo.load_linkage_state(linkage.id)
                if gate is None:
                    continue
                desired = GATE_ACTIVE if linkage.fail_safe or link.mode == MODE_NORMAL else GATE_PAUSED
                if desired != gate.desired:
                    gate.desired = desired
                    self._repo.save_linkage_state(gate)
                self._reconcile_gate(link, linkage, gate, now)

        # Close the episode once NORMAL and every ordinary gate is active.
        if link.mode == MODE_NORMAL:
            pending = [
                gate.id
                for linkage in self._repo.list_linkages(link.id)
                if (gate := self._repo.load_linkage_state(linkage.id)) is not None and gate.actual != GATE_ACTIVE
            ]
            if not pending:
                self._close_episode(link, now, "all linkages active")

    def _reconcile_gate(self, link: LinkState, linkage: Linkage, gate: LinkageState, now: int) -> bool:
        """Drive one gate toward its desired state.

        Returns ``True`` when a failed resume forced a rollback while the
        caller was iterating.
        """

        action = ACTION_PAUSE if gate.desired == GATE_PAUSED else ACTION_RESUME
        if gate.actual == gate.desired:
            gate.updated_at = now
            self._repo.save_linkage_state(gate)
            return False

        command = self._ensure_command(link, linkage, action, now)
        gate.command_id = command.id
        self._repo.save_linkage_state(gate)
        outcome = self._deliver(command, link, linkage, action, now)
        if outcome == "acked":
            gate.actual = GATE_PAUSED if action == ACTION_PAUSE else GATE_ACTIVE
            self._repo.save_linkage_state(gate)
            return False
        if outcome == "failed-pause":
            gate.actual = GATE_PAUSE_FAILED
            self._repo.save_linkage_state(gate)
            return False
        if outcome == "failed-resume":
            gate.actual = GATE_RESUME_FAILED
            self._repo.save_linkage_state(gate)
            # Intent is now PAUSED for every gate; the second pass issues it.
            gate.desired = GATE_PAUSED
            self._repo.save_linkage_state(gate)
            return True
        return False  # stale command; next pass rebuilds it under the new token

    def _ensure_command(
        self,
        link: LinkState,
        linkage: Linkage,
        action: str,
        now: int,
    ) -> LinkCommand:
        episode = self._repo.open_episode(link.id)
        episode_id = episode.id if episode is not None else "steady"
        key = f"{link.id}:{linkage.id}:{action}:t{link.fencing_token}"
        existing = self._repo.command_by_key(key)
        if existing is not None:
            return existing
        command = LinkCommand(
            id=uuid.uuid4().hex,
            key=key,
            link_id=link.id,
            linkage_id=linkage.id,
            action=action,
            status=CMD_PENDING,
            token=link.fencing_token,
            episode_id=episode_id,
            issued_at=now,
        )
        self._repo.save_command(command)
        return command

    def _deliver(self, command: LinkCommand, link: LinkState, linkage: Linkage, action: str, now: int) -> str:
        if command.status == CMD_ACKED:
            return "acked"
        try:
            if action == ACTION_PAUSE:
                self._actuator.pause(link.id, linkage.id, command.token)
            else:
                self._actuator.resume(link.id, linkage.id, command.token)
        except StaleCommand as exc:
            command.status = CMD_STALE
            command.detail = str(exc)
            self._repo.save_command(command)
            return "stale"
        except ActuatorFailure as exc:
            command.status = CMD_FAILED
            command.attempts += 1
            command.detail = str(exc)
            self._repo.save_command(command)
            if action == ACTION_PAUSE:
                # A linkage that would not stop is an operator problem; stay
                # degraded and keep the alarm visible until it succeeds.
                self._event(
                    EV_PAUSE_FAILED,
                    ENTITY_LINKAGE,
                    linkage.id,
                    str(exc),
                    {"attempts": command.attempts},
                )
                return "failed-pause"
            # A linkage that would not restart: whole recovery rolls back.
            self._event(
                EV_RESUME_FAILED,
                ENTITY_LINKAGE,
                linkage.id,
                str(exc),
                {"attempts": command.attempts},
            )
            self._rollback_recovery(link, now, f"resume of {linkage.id} failed: {exc}")
            return "failed-resume"
        command.status = CMD_ACKED
        command.detail = f"{action} accepted under token {command.token}"
        self._repo.save_command(command)
        return "acked"

    def _rollback_recovery(self, link: LinkState, now: int, reason: str) -> None:
        """Force the link back to degraded and invalidate the current epoch.

        Used when a resume command is rejected even though the link itself is
        still reachable: data is back but a control linkage is not, so normal
        mode cannot be claimed.
        """

        link.mode = MODE_DEGRADED
        link.availability = LINK_DOWN
        link.stable_count = 0
        link.stable_advanced_at = None
        link.fencing_token += 1
        link.since = now
        link.reason = f"rolled back: {reason}"
        self._repo.save_link_state(link)
        self._event(EV_ROLLBACK, ENTITY_LINK, link.id, reason, {"token": link.fencing_token})
        episode = self._repo.open_episode(link.id) or self._repo.reopen_latest_episode(link.id, reason)
        if episode is None:
            episode = self._new_episode(link.id, reason)
            self._repo.save_episode(episode)
            self._event(EV_EPISODE_OPENED, ENTITY_EPISODE, episode.id, reason, {"link_id": link.id})

    # -- operator actions ---------------------------------------------------

    def hold(self, link_id: str, actor: str = "operator", reason: str = "") -> LinkState:
        """Suspend automatic recovery while leaving availability untouched.

        Holding a healthy link parks it in degraded (linkages stay paused);
        holding an already degraded link only prevents the auto-recovery.
        """

        with self._lock:
            link = self._require_link_state(link_id)
            now = self._clock()
            was_held = link.hold
            link.hold = True
            if link.mode != MODE_DEGRADED:
                link.mode = MODE_DEGRADED
                link.stable_count = 0
                link.stable_advanced_at = None
                link.fencing_token += 1
                link.since = now
                episode = self._new_episode(link_id, "operator hold")
                self._repo.save_episode(episode)
                self._event(EV_EPISODE_OPENED, ENTITY_EPISODE, episode.id, episode.reason, {"link_id": link_id})
            link.reason = f"operator hold: {reason or 'no reason given'}"
            link.version += 1
            link.updated_at = now
            self._repo.save_link_state(link)
            if not was_held:
                self._event(EV_HOLD, ENTITY_LINK, link_id, link.reason, actor=actor)
            self._reconcile_linkages(link, now)
            return self._require_link_state(link_id)

    def release_hold(self, link_id: str, actor: str = "operator") -> LinkState:
        """Clear the hold; idempotent when no hold was set."""

        with self._lock:
            link = self._require_link_state(link_id)
            if not link.hold:
                return link
            link.hold = False
            link.version += 1
            self._repo.save_link_state(link)
            self._event(EV_HOLD_CLEARED, ENTITY_LINK, link_id, "operator released hold", actor=actor)
            return self.tick(link_id)

    def recover(self, link_id: str, actor: str = "operator", at: int | None = None) -> LinkState:
        """Manual confirmation path: clear a hold and run the same recovery.

        Deliberately shares the automatic path so manual and automatic
        recovery have identical stability, rollback and idempotency rules.
        """

        with self._lock:
            now = at if at is not None else self._clock()
            link = self._require_link_state(link_id)
            if link.hold:
                link.hold = False
                self._repo.save_link_state(link)
                self._event(EV_HOLD_CLEARED, ENTITY_LINK, link_id, "hold cleared by manual recovery", actor=actor)
            return self.tick(link_id, now)

    # -- episodes -----------------------------------------------------------

    def _new_episode(self, link_id: str, reason: str) -> Episode:
        return Episode(
            id=uuid.uuid4().hex,
            link_id=link_id,
            status=EPISODE_OPEN,
            opened_at=self._clock(),
            reason=reason,
        )

    def _close_episode(self, link: LinkState, now: int, reason: str) -> None:
        episode = self._repo.open_episode(link.id)
        if episode is None or episode.status == EPISODE_CLOSED:
            return
        closed = Episode(
            id=episode.id,
            link_id=episode.link_id,
            status=EPISODE_CLOSED,
            opened_at=episode.opened_at,
            closed_at=now,
            reason=episode.reason,
            close_reason=reason,
        )
        self._repo.save_episode(closed)
        self._event(
            EV_EPISODE_CLOSED,
            ENTITY_EPISODE,
            closed.id,
            reason,
            {"link_id": link.id, "duration": now - episode.opened_at},
        )

    # -- read models --------------------------------------------------------

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            links = []
            for link in self._repo.list_link_states():
                config = self._repo.load_link_config(link.id)
                devices = []
                for device_config in self._repo.list_device_configs(link.id):
                    state = self._repo.load_device_state(device_config.id)
                    devices.append(
                        {
                            "id": device_config.id,
                            "name": device_config.name,
                            "critical": device_config.critical,
                            **(state.as_dict() if state else {}),
                        }
                    )
                linkages = []
                for linkage_config in self._repo.list_linkages(link.id):
                    gate = self._repo.load_linkage_state(linkage_config.id)
                    linkages.append(
                        {
                            "id": linkage_config.id,
                            "name": linkage_config.name,
                            "fail_safe": linkage_config.fail_safe,
                            **(gate.as_dict() if gate else {}),
                        }
                    )
                links.append(
                    {
                        "link": config.as_dict() if config else {"id": link.id},
                        "state": link.as_dict(),
                        "devices": devices,
                        "linkages": linkages,
                    }
                )
            return {"links": links, "stats": self._repo.stats()}

    def audit_trail(
        self,
        entity_type: str | None = None,
        entity_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, object]]:
        with self._lock:
            events = self._repo.list_events(entity_type, entity_id)
            if limit is not None:
                events = events[-limit:]
            return [event.as_dict() for event in reversed(events)]

    def commands(self, link_id: str | None = None) -> list[dict[str, object]]:
        with self._lock:
            return [command.as_dict() for command in self._repo.list_commands(link_id)]

    def episodes(self, link_id: str | None = None) -> list[dict[str, object]]:
        with self._lock:
            return [episode.as_dict() for episode in self._repo.list_episodes(link_id)]

    # -- internals ----------------------------------------------------------

    def _persist_device_events(self, device_id: str, events: Sequence[tuple[str, str, dict[str, object]]]) -> None:
        for kind, detail, evidence in events:
            self._event(kind, ENTITY_DEVICE, device_id, detail, evidence)

    def _event(
        self,
        kind: str,
        entity_type: str,
        entity_id: str,
        detail: str,
        evidence: dict[str, object] | None = None,
        actor: str = "system",
    ) -> LinkEvent:
        event = LinkEvent(
            id=uuid.uuid4().hex,
            at=self._eval_at if self._eval_at is not None else self._clock(),
            kind=kind,
            entity_type=entity_type,
            entity_id=entity_id,
            detail=detail,
            evidence=evidence or {},
            actor=actor,
        )
        self._repo.append_event(event)
        return event

    def _require_link_config(self, link_id: str) -> LinkConfig:
        config = self._repo.load_link_config(link_id)
        if config is None:
            raise ValueError(f"unknown link: {link_id}")
        return config

    def _require_config_for_device(self, device_id: str) -> LinkConfig:
        device = self._repo.load_device_config(device_id)
        if device is None:
            raise ValueError(f"unknown device: {device_id}")
        return self._require_link_config(device.link_id)

    def _require_link_state(self, link_id: str) -> LinkState:
        state = self._repo.load_link_state(link_id)
        if state is None:
            raise ValueError(f"unknown link: {link_id}")
        return state

    def _require_device_state(self, device_id: str) -> DeviceState:
        state = self._repo.load_device_state(device_id)
        if state is None:
            state = DeviceState(id=device_id, link_id="", entered_at=self._clock())
        return state


# imported here to avoid a policy -> monitor dependency cycle
def _evaluate(state: DeviceState, config: LinkConfig, now: int, *, heartbeat_seq: int | None = None):
    from .policy import evaluate_device

    return evaluate_device(state, config, now, heartbeat_seq=heartbeat_seq)
