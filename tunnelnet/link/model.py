"""Domain objects for link online monitoring and degraded operation.

The object graph is small enough to keep in one module:

* :class:`DeviceConfig` / :class:`DeviceState` - one collection device on a link
* :class:`LinkConfig` / :class:`LinkState`   - one uplink and its run mode
* :class:`Linkage`                            - a control linkage gated by the link
* :class:`LinkEvent`                          - one append-only audit entry
* :class:`LinkCommand`                        - one pause/resume command in the outbox

Every object is JSON round-trippable because the state is persisted as documents
under the file backed store, one document per identifier.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Device status values.  SUSPECT covers the gap between one missed heartbeat and
# a confirmed OFFLINE; JITTER marks a device that flaps faster than the window.
ONLINE = "online"
SUSPECT = "suspect"
OFFLINE = "offline"
JITTER = "jitter"

DEVICE_STATUSES = (ONLINE, SUSPECT, OFFLINE, JITTER)

# Statuses that mean "no trustworthy data from this device right now".
UNAVAILABLE_STATUSES = (OFFLINE, JITTER)

# Link availability and run modes.
LINK_UP = "up"
LINK_DOWN = "down"

MODE_NORMAL = "normal"
MODE_DEGRADED = "degraded"
MODE_RECOVERING = "recovering"

# Linkage gate states and command outcomes.
GATE_ACTIVE = "active"
GATE_PAUSED = "paused"
GATE_PAUSE_PENDING = "pause_pending"
GATE_PAUSE_FAILED = "pause_failed"
GATE_RESUME_PENDING = "resume_pending"
GATE_RESUME_FAILED = "resume_failed"

ACTION_PAUSE = "pause"
ACTION_RESUME = "resume"

CMD_PENDING = "pending"
CMD_SENT = "sent"
CMD_ACKED = "acked"
CMD_STALE = "stale"
CMD_FAILED = "failed"

# Degradation episode lifecycle.
EPISODE_OPEN = "open"
EPISODE_ROLLBACK = "rollback"
EPISODE_CLOSED = "closed"


@dataclass(frozen=True)
class DeviceConfig:
    """Static identity of one collection device on one link."""

    id: str
    link_id: str
    name: str = ""
    critical: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "link_id": self.link_id,
            "name": self.name or self.id,
            "critical": self.critical,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "DeviceConfig":
        return cls(
            id=str(payload["id"]),
            link_id=str(payload["link_id"]),
            name=str(payload.get("name", "")),
            critical=bool(payload.get("critical", False)),
        )


@dataclass(frozen=True)
class LinkConfig:
    """Tunable thresholds for one link.

    ``miss_limit`` is the number of consecutive missed heartbeats that confirms
    an OFFLINE device; ``miss_limit - 1`` missed heartbeats leave the device in
    SUSPECT.  ``flap_window`` / ``flap_limit`` define the jitter detector and
    ``stable_ticks`` is the quiet period required to exit JITTER or recover.
    """

    id: str
    name: str = ""
    miss_limit: int = 3
    heartbeat_period: int = 10
    flap_window: int = 60
    flap_limit: int = 3
    offline_ratio: float = 0.34
    stable_ticks: int = 3

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name or self.id,
            "miss_limit": self.miss_limit,
            "heartbeat_period": self.heartbeat_period,
            "flap_window": self.flap_window,
            "flap_limit": self.flap_limit,
            "offline_ratio": self.offline_ratio,
            "stable_ticks": self.stable_ticks,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "LinkConfig":
        return cls(
            id=str(payload["id"]),
            name=str(payload.get("name", "")),
            miss_limit=int(payload.get("miss_limit", 3)),
            heartbeat_period=int(payload.get("heartbeat_period", 10)),
            flap_window=int(payload.get("flap_window", 60)),
            flap_limit=int(payload.get("flap_limit", 3)),
            offline_ratio=float(payload.get("offline_ratio", 0.34)),
            stable_ticks=int(payload.get("stable_ticks", 3)),
        )


@dataclass
class DeviceState:
    """Mutable runtime status of one device, persisted after every evaluation."""

    id: str
    link_id: str
    status: str = OFFLINE
    last_seq: int = -1
    last_heartbeat_at: int | None = None
    missed: int = 0
    entered_at: int = 0
    # tick timestamps of ONLINE->SUSPECT edges inside the rolling flap window
    flap_at: list[int] = field(default_factory=list)
    last_drop_at: int | None = None
    flapping: bool = False
    stable_since: int | None = None
    updated_at: int = 0
    note: str = "never seen"
    version: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "link_id": self.link_id,
            "status": self.status,
            "last_seq": self.last_seq,
            "last_heartbeat_at": self.last_heartbeat_at,
            "missed": self.missed,
            "entered_at": self.entered_at,
            "flap_at": list(self.flap_at),
            "last_drop_at": self.last_drop_at,
            "flapping": self.flapping,
            "stable_since": self.stable_since,
            "updated_at": self.updated_at,
            "note": self.note,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "DeviceState":
        return cls(
            id=str(payload["id"]),
            link_id=str(payload["link_id"]),
            status=str(payload.get("status", OFFLINE)),
            last_seq=int(payload.get("last_seq", -1)),
            last_heartbeat_at=payload.get("last_heartbeat_at") and int(payload["last_heartbeat_at"]),
            missed=int(payload.get("missed", 0)),
            entered_at=int(payload.get("entered_at", 0)),
            flap_at=[int(value) for value in payload.get("flap_at", [])],  # type: ignore[arg-type]
            last_drop_at=payload.get("last_drop_at") and int(payload["last_drop_at"]),
            flapping=bool(payload.get("flapping", False)),
            stable_since=payload.get("stable_since") and int(payload["stable_since"]),
            updated_at=int(payload.get("updated_at", 0)),
            note=str(payload.get("note", "")),
            version=int(payload.get("version", 0)),
        )


@dataclass
class LinkState:
    """Mutable run mode of one link, persisted before any linkage action."""

    id: str
    availability: str = LINK_DOWN
    mode: str = MODE_DEGRADED
    hold: bool = False
    stable_count: int = 0
    stable_advanced_at: int | None = None
    since: int = 0
    fencing_token: int = 0
    offline_devices: list[str] = field(default_factory=list)
    unavailable_ratio: float = 0.0
    reason: str = "bootstrapped degraded until heartbeats prove the link up"
    updated_at: int = 0
    version: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "availability": self.availability,
            "mode": self.mode,
            "hold": self.hold,
            "stable_count": self.stable_count,
            "stable_advanced_at": self.stable_advanced_at,
            "since": self.since,
            "fencing_token": self.fencing_token,
            "offline_devices": list(self.offline_devices),
            "unavailable_ratio": self.unavailable_ratio,
            "reason": self.reason,
            "updated_at": self.updated_at,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "LinkState":
        return cls(
            id=str(payload["id"]),
            availability=str(payload.get("availability", LINK_DOWN)),
            mode=str(payload.get("mode", MODE_DEGRADED)),
            hold=bool(payload.get("hold", False)),
            stable_count=int(payload.get("stable_count", 0)),
            stable_advanced_at=(
                None if payload.get("stable_advanced_at") is None else int(payload["stable_advanced_at"])
            ),
            since=int(payload.get("since", 0)),
            fencing_token=int(payload.get("fencing_token", 0)),
            offline_devices=[str(value) for value in payload.get("offline_devices", [])],  # type: ignore[arg-type]
            unavailable_ratio=float(payload.get("unavailable_ratio", 0.0)),
            reason=str(payload.get("reason", "")),
            updated_at=int(payload.get("updated_at", 0)),
            version=int(payload.get("version", 0)),
        )


@dataclass(frozen=True)
class Linkage:
    """A control linkage whose automatic action is gated by a link.

    ``fail_safe`` linkages (alarms, emergency ventilation) are never paused:
    losing data must not blind the operator to hazards.  Only ordinary data
    driven automatic linkages are suspended while the link is degraded.
    """

    id: str
    link_id: str
    name: str = ""
    fail_safe: bool = False

    def as_dict(self) -> dict[str, object]:
        return {"id": self.id, "link_id": self.link_id, "name": self.name or self.id, "fail_safe": self.fail_safe}

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "Linkage":
        return cls(
            id=str(payload["id"]),
            link_id=str(payload["link_id"]),
            name=str(payload.get("name", "")),
            fail_safe=bool(payload.get("fail_safe", False)),
        )


@dataclass
class LinkageState:
    """Desired gate, reconciled actual gate and the current command attempt."""

    id: str
    link_id: str
    desired: str = GATE_ACTIVE
    actual: str = GATE_ACTIVE
    command_id: str = ""
    updated_at: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "link_id": self.link_id,
            "desired": self.desired,
            "actual": self.actual,
            "command_id": self.command_id,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "LinkageState":
        return cls(
            id=str(payload["id"]),
            link_id=str(payload["link_id"]),
            desired=str(payload.get("desired", GATE_ACTIVE)),
            actual=str(payload.get("actual", GATE_ACTIVE)),
            command_id=str(payload.get("command_id", "")),
            updated_at=int(payload.get("updated_at", 0)),
        )


@dataclass(frozen=True)
class LinkEvent:
    """One append-only audit entry.  Console and audit API read the same log."""

    id: str
    at: int
    kind: str
    entity_type: str
    entity_id: str
    detail: str = ""
    evidence: dict[str, object] = field(default_factory=dict)
    actor: str = "system"

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "at": self.at,
            "kind": self.kind,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "detail": self.detail,
            "evidence": dict(self.evidence),
            "actor": self.actor,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "LinkEvent":
        evidence = payload.get("evidence", {})
        return cls(
            id=str(payload["id"]),
            at=int(payload["at"]),
            kind=str(payload["kind"]),
            entity_type=str(payload["entity_type"]),
            entity_id=str(payload["entity_id"]),
            detail=str(payload.get("detail", "")),
            evidence=dict(evidence) if isinstance(evidence, dict) else {},  # type: ignore[arg-type]
            actor=str(payload.get("actor", "system")),
        )


@dataclass(frozen=True)
class Episode:
    """One degradation lifecycle: opened when a link goes down, closed on clean resume."""

    id: str
    link_id: str
    status: str
    opened_at: int
    closed_at: int | None = None
    reason: str = ""
    close_reason: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "link_id": self.link_id,
            "status": self.status,
            "opened_at": self.opened_at,
            "closed_at": self.closed_at,
            "reason": self.reason,
            "close_reason": self.close_reason,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "Episode":
        closed_at = payload.get("closed_at")
        return cls(
            id=str(payload["id"]),
            link_id=str(payload["link_id"]),
            status=str(payload["status"]),
            opened_at=int(payload["opened_at"]),
            closed_at=None if closed_at is None else int(closed_at),
            reason=str(payload.get("reason", "")),
            close_reason=str(payload.get("close_reason", "")),
        )


@dataclass()
class LinkCommand:
    """One pause/resume command.

    The ``key`` makes delivery idempotent (a re-run is a no-op); the fencing
    token invalidates every stale command after a rollback bumps the epoch.
    """

    id: str
    key: str
    link_id: str
    linkage_id: str
    action: str
    status: str
    token: int
    episode_id: str
    issued_at: int
    attempts: int = 0
    detail: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "key": self.key,
            "link_id": self.link_id,
            "linkage_id": self.linkage_id,
            "action": self.action,
            "status": self.status,
            "token": self.token,
            "episode_id": self.episode_id,
            "issued_at": self.issued_at,
            "attempts": self.attempts,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "LinkCommand":
        return cls(
            id=str(payload["id"]),
            key=str(payload["key"]),
            link_id=str(payload["link_id"]),
            linkage_id=str(payload["linkage_id"]),
            action=str(payload["action"]),
            status=str(payload["status"]),
            token=int(payload["token"]),
            episode_id=str(payload["episode_id"]),
            issued_at=int(payload["issued_at"]),
            attempts=int(payload.get("attempts", 0)),
            detail=str(payload.get("detail", "")),
        )
