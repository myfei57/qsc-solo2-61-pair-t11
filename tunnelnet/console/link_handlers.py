"""Console endpoints for link monitoring and degraded operation.

The console and the audit trail read the same persisted documents:
``GET /api/links`` returns the live projection and ``GET /api/links/audit``
returns the event log behind it, both loaded through the repository.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tunnelnet.link import (
    JITTER,
    OFFLINE,
    ONLINE,
    SUSPECT,
    DeviceConfig,
    LinkConfig,
    Linkage,
)

from .http import Request, RequestError, Response, json_response

if TYPE_CHECKING:  # pragma: no cover - imported only for type checkers
    from .server import Server

DEVICE_STATUS_LABELS = {
    ONLINE: "online",
    SUSPECT: "suspected loss (one heartbeat overdue, controls still trusted)",
    OFFLINE: "offline (consecutive heartbeats lost)",
    JITTER: "jitter (rapid up/down oscillation, treated as unavailable)",
}


def links_overview(server: "Server", request: Request) -> Response:
    return json_response(server.runtime.links.snapshot())


def links_status_legend(server: "Server", request: Request) -> Response:
    return json_response(
        {
            "device_status": DEVICE_STATUS_LABELS,
            "offline_policy": (
                "SUSPECT after one missed heartbeat; OFFLINE only after miss_limit "
                "consecutive misses; JITTER after flap_limit drops inside the flap window"
            ),
            "recovery_policy": (
                "automatic after stable_ticks healthy evaluations; a hold pauses the "
                "auto path; a failed resume rolls the whole link back"
            ),
        }
    )


def links_heartbeat(server: "Server", request: Request) -> Response:
    device_id = request.str_field("device_id")
    if not device_id:
        raise RequestError(400, "device_id is required")
    seq = request.int_field("seq", 0)
    if seq <= 0:
        raise RequestError(400, "seq must be a positive integer")
    at = request.payload.get("at")
    at_value = int(at) if isinstance(at, (int, float)) and not isinstance(at, bool) else None
    state = server.runtime.links.heartbeat(device_id, seq, at_value)
    return json_response({"device": state.as_dict()})


def links_tick(server: "Server", request: Request) -> Response:
    at = request.payload.get("at")
    at_value = int(at) if isinstance(at, (int, float)) and not isinstance(at, bool) else None
    link_id = request.str_field("link_id")
    if link_id:
        states = [server.runtime.links.tick(link_id, at_value)]
    else:
        states = server.runtime.links.tick_all(at_value)
    return json_response({"links": [state.as_dict() for state in states]})


def links_hold(server: "Server", request: Request) -> Response:
    link_id = request.str_field("link_id")
    if not link_id:
        raise RequestError(400, "link_id is required")
    state = server.runtime.links.hold(
        link_id, actor=request.str_field("actor", "operator"), reason=request.str_field("reason", "")
    )
    return json_response({"link": state.as_dict()})


def links_release(server: "Server", request: Request) -> Response:
    link_id = request.str_field("link_id")
    if not link_id:
        raise RequestError(400, "link_id is required")
    state = server.runtime.links.release_hold(link_id, actor=request.str_field("actor", "operator"))
    return json_response({"link": state.as_dict()})


def links_recover(server: "Server", request: Request) -> Response:
    link_id = request.str_field("link_id")
    if not link_id:
        raise RequestError(400, "link_id is required")
    at = request.payload.get("at")
    at_value = int(at) if isinstance(at, (int, float)) and not isinstance(at, bool) else None
    state = server.runtime.links.recover(link_id, actor=request.str_field("actor", "operator"), at=at_value)
    return json_response({"link": state.as_dict()})


def _query_value(request: Request, name: str) -> str | None:
    raw = request.query.get(name)
    if not raw:
        return None
    value = raw[0] if isinstance(raw, (list, tuple)) else raw
    return value if isinstance(value, str) else str(value)


def links_audit(server: "Server", request: Request) -> Response:
    entity_type = _query_value(request, "entity_type")
    entity_id = _query_value(request, "entity_id")
    limit_raw = _query_value(request, "limit") or ""
    limit = int(limit_raw) if limit_raw.isdigit() else None
    return json_response({"events": server.runtime.links.audit_trail(entity_type, entity_id, limit)})


def links_commands(server: "Server", request: Request) -> Response:
    link_id = _query_value(request, "link_id")
    return json_response({"commands": server.runtime.links.commands(link_id)})


def links_episodes(server: "Server", request: Request) -> Response:
    link_id = _query_value(request, "link_id")
    return json_response({"episodes": server.runtime.links.episodes(link_id)})


def links_register_link(server: "Server", request: Request) -> Response:
    link_id = request.str_field("id")
    if not link_id:
        raise RequestError(400, "id is required")
    config = LinkConfig(
        id=link_id,
        name=request.str_field("name", ""),
        miss_limit=request.int_field("miss_limit", 3),
        heartbeat_period=request.int_field("heartbeat_period", 10),
        flap_window=request.int_field("flap_window", 60),
        flap_limit=request.int_field("flap_limit", 3),
        offline_ratio=request.float_field("offline_ratio", 0.34),
        stable_ticks=request.int_field("stable_ticks", 3),
    )
    if config.miss_limit < 1:
        raise RequestError(400, "miss_limit must be >= 1")
    if config.heartbeat_period < 1 or config.flap_window < 1 or config.stable_ticks < 1:
        raise RequestError(400, "heartbeat_period, flap_window and stable_ticks must be >= 1")
    if not 0 < config.offline_ratio <= 1:
        raise RequestError(400, "offline_ratio must be within (0, 1]")
    state = server.runtime.links.ensure_link(config)
    return json_response({"link": state.as_dict(), "config": config.as_dict()})


def links_register_device(server: "Server", request: Request) -> Response:
    device_id = request.str_field("id")
    link_id = request.str_field("link_id")
    if not device_id or not link_id:
        raise RequestError(400, "id and link_id are required")
    device = DeviceConfig(
        id=device_id,
        link_id=link_id,
        name=request.str_field("name", ""),
        critical=bool(request.payload.get("critical", False)),
    )
    state = server.runtime.links.ensure_device(device)
    return json_response({"device": state.as_dict()})


def links_register_linkage(server: "Server", request: Request) -> Response:
    linkage_id = request.str_field("id")
    link_id = request.str_field("link_id")
    if not linkage_id or not link_id:
        raise RequestError(400, "id and link_id are required")
    linkage = Linkage(
        id=linkage_id,
        link_id=link_id,
        name=request.str_field("name", ""),
        fail_safe=bool(request.payload.get("fail_safe", False)),
    )
    state = server.runtime.links.ensure_linkage(linkage)
    return json_response({"linkage": state.as_dict()})
