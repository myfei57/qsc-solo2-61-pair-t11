"""Link online monitoring and degraded-operation control.

Public surface:

* :class:`LinkMonitor` - heartbeat ingestion, mode state machine, linkage
  pause/resume reconciliation with fencing, idempotency and rollback;
* :class:`LinkRepository` - file-backed persistence shared by the console and
  the audit API;
* :class:`RecordingActuator` - the in-process command sink;
* the dataclasses and status constants in :mod:`tunnelnet.link.model`;
* the pure evaluators in :mod:`tunnelnet.link.policy`.
"""

from . import model, policy
from .model import (
    ACTION_PAUSE,
    ACTION_RESUME,
    CMD_ACKED,
    CMD_FAILED,
    CMD_PENDING,
    CMD_STALE,
    DEVICE_STATUSES,
    GATE_ACTIVE,
    GATE_PAUSED,
    GATE_PAUSE_FAILED,
    GATE_PAUSE_PENDING,
    GATE_RESUME_FAILED,
    GATE_RESUME_PENDING,
    JITTER,
    LINK_DOWN,
    LINK_UP,
    MODE_DEGRADED,
    MODE_NORMAL,
    MODE_RECOVERING,
    OFFLINE,
    ONLINE,
    SUSPECT,
    UNAVAILABLE_STATUSES,
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
from .monitor import (
    ActuatorFailure,
    LinkActuator,
    LinkMonitor,
    RecordingActuator,
    StaleCommand,
)
from .gate import LinkageGate
from .repository import LinkRepository
from .scanner import LinkScanner

__all__ = [
    "ACTION_PAUSE",
    "ACTION_RESUME",
    "DEVICE_STATUSES",
    "GATE_ACTIVE",
    "GATE_PAUSED",
    "GATE_PAUSE_FAILED",
    "GATE_PAUSE_PENDING",
    "GATE_RESUME_FAILED",
    "GATE_RESUME_PENDING",
    "JITTER",
    "LINK_DOWN",
    "LINK_UP",
    "MODE_DEGRADED",
    "MODE_NORMAL",
    "MODE_RECOVERING",
    "OFFLINE",
    "ONLINE",
    "SUSPECT",
    "UNAVAILABLE_STATUSES",
    "ActuatorFailure",
    "DeviceConfig",
    "DeviceState",
    "Episode",
    "LinkActuator",
    "LinkCommand",
    "LinkConfig",
    "LinkEvent",
    "LinkMonitor",
    "LinkState",
    "LinkRepository",
    "LinkScanner",
    "Linkage",
    "LinkageGate",
    "LinkageState",
    "RecordingActuator",
    "StaleCommand",
    "model",
    "policy",
]
