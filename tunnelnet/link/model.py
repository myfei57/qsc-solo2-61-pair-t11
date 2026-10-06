"""State model for link online monitoring and degraded running.

A *device* is one collector in the corridor. A *link* is the transport path a
group of devices share (gateway / loop). Device state is derived from missed
heartbeats; link state is derived from the state of its devices and drives the
degraded mode of the control system.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class DeviceState(str, Enum):
    """Heartbeat derived state of one collector."""

    UNKNOWN = "unknown"
    ONLINE = "online"
    JITTER = "jitter"
    OFFLINE = "offline"

    def __str__(self) -> str:
        return self.value


class LinkState(str, Enum):
    """Aggregate state of one transport link."""

    ONLINE = "online"
    DEGRADED = "degraded"
    RECOVERING = "recovering"

    def __str__(self) -> str:
        return self.value


class LinkageClass(str, Enum):
    """Whether a linkage may be paused while the link is degraded.

    Safety linkages (fire suppression, damper close) are never paused: when we
    cannot trust fresh data we keep the protective action armed. Only ordinary
    automatic linkages are suspended.
    """

    NORMAL = "normal"
    SAFETY = "safety"

    def __str__(self) -> str:
        return self.value


# Default heartbeat policy. Detection latency worst case is
# ``miss_threshold * interval`` seconds.
DEFAULT_INTERVAL = 10.0
DEFAULT_MISS_THRESHOLD = 3
DEFAULT_RECOVER_HITS = 2
DEFAULT_OFFLINE_RATIO = 0.5
DEFAULT_RECOVER_STABLE_SECONDS = 30.0
DEFAULT_SWEEP_SECONDS = 5.0


@dataclass
class Device:
    """One registered collector and its latest heartbeat view."""

    id: str
    link_id: str
    state: DeviceState = DeviceState.UNKNOWN
    last_seen: float | None = None
    missed: int = 0
    recover_hits: int = 0
    flaps: int = 0
    registered_at: float = 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "link_id": self.link_id,
            "state": self.state.value,
            "last_seen": self.last_seen,
            "missed": self.missed,
            "recover_hits": self.recover_hits,
            "flaps": self.flaps,
            "registered_at": self.registered_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "Device":
        return cls(
            id=str(payload.get("id", "")),
            link_id=str(payload.get("link_id", "")),
            state=DeviceState(str(payload.get("state", "unknown"))),
            last_seen=_optional_float(payload.get("last_seen")),
            missed=int(payload.get("missed", 0)),
            recover_hits=int(payload.get("recover_hits", 0)),
            flaps=int(payload.get("flaps", 0)),
            registered_at=float(payload.get("registered_at", 0.0)),
        )


@dataclass
class Link:
    """One transport link and the heartbeat policy that governs its devices."""

    id: str
    name: str
    state: LinkState = LinkState.ONLINE
    interval: float = DEFAULT_INTERVAL
    miss_threshold: int = DEFAULT_MISS_THRESHOLD
    recover_hits: int = DEFAULT_RECOVER_HITS
    offline_ratio: float = DEFAULT_OFFLINE_RATIO
    recover_stable_seconds: float = DEFAULT_RECOVER_STABLE_SECONDS
    degraded_since: float | None = None
    recovering_since: float | None = None
    episode: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "state": self.state.value,
            "interval": self.interval,
            "miss_threshold": self.miss_threshold,
            "recover_hits": self.recover_hits,
            "offline_ratio": self.offline_ratio,
            "recover_stable_seconds": self.recover_stable_seconds,
            "degraded_since": self.degraded_since,
            "recovering_since": self.recovering_since,
            "episode": self.episode,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "Link":
        return cls(
            id=str(payload.get("id", "")),
            name=str(payload.get("name", "")),
            state=LinkState(str(payload.get("state", "online"))),
            interval=float(payload.get("interval", DEFAULT_INTERVAL)),
            miss_threshold=int(payload.get("miss_threshold", DEFAULT_MISS_THRESHOLD)),
            recover_hits=int(payload.get("recover_hits", DEFAULT_RECOVER_HITS)),
            offline_ratio=float(payload.get("offline_ratio", DEFAULT_OFFLINE_RATIO)),
            recover_stable_seconds=float(
                payload.get("recover_stable_seconds", DEFAULT_RECOVER_STABLE_SECONDS)
            ),
            degraded_since=_optional_float(payload.get("degraded_since")),
            recovering_since=_optional_float(payload.get("recovering_since")),
            episode=int(payload.get("episode", 0)),
        )


@dataclass(frozen=True)
class Linkage:
    """One controllable linkage and whether degradation may suspend it."""

    key: str
    name: str
    stage: str
    linkage_class: LinkageClass = LinkageClass.NORMAL
    paused: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "name": self.name,
            "stage": self.stage,
            "class": self.linkage_class.value,
            "paused": self.paused,
        }


@dataclass(frozen=True)
class Transition:
    """One state change emitted by the monitor and written to the audit trail."""

    scope: str
    target_id: str
    old: str
    new: str
    at: float
    episode: int = 0
    reason: str = ""
    seq: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "scope": self.scope,
            "target_id": self.target_id,
            "old": self.old,
            "new": self.new,
            "at": self.at,
            "episode": self.episode,
            "reason": self.reason,
            "seq": self.seq,
        }

    def event(self) -> str:
        """Stable audit event name, e.g. ``link_degraded`` / ``device_offline``."""

        return f"{self.scope}_{self.new}"


@dataclass(frozen=True)
class RecoveryResult:
    """Outcome of a confirm or rollback request, used by the console."""

    link_id: str
    state: LinkState
    episode: int
    restored: tuple[str, ...] = field(default_factory=tuple)
    rolled_back: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "link_id": self.link_id,
            "state": self.state.value,
            "episode": self.episode,
            "restored": list(self.restored),
            "rolled_back": self.rolled_back,
        }


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    return float(value)
