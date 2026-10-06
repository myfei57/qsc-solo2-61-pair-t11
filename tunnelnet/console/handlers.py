"""HTTP handlers for every console endpoint."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tunnelnet.audit import between, events, filter_records, last, most_recent
from tunnelnet.cabin import Cabin, status_summary
from tunnelnet.cable import danger_count, healthy_count, load_factor
from tunnelnet.drain import Pit
from tunnelnet.env import snapshot as env_snapshot
from tunnelnet.fire import Detector
from tunnelnet.link import (
    DEFAULT_INTERVAL as DEFAULT_LINK_INTERVAL,
    DEFAULT_MISS_THRESHOLD as DEFAULT_LINK_MISS,
    DEFAULT_OFFLINE_RATIO as DEFAULT_LINK_RATIO,
    DEFAULT_RECOVER_HITS as DEFAULT_LINK_HITS,
    DEFAULT_RECOVER_STABLE_SECONDS as DEFAULT_LINK_STABLE,
    summarize_devices,
)
from tunnelnet.ns import Stage, control_chain
from tunnelnet.reporting import audit_export, telemetry_export, to_csv
from tunnelnet.reporting import store_export as store_csv_export
from tunnelnet.store import export_all, export_kind, list_approvals, revoke_approval, summarize

from . import checks, ops
from . import describe as describe_module
from . import snapshot as snapshot_module
from . import telemetry as telemetry_module
from .audit_log import record_event
from .cycle import run_cycle
from .http import Request, RequestError, Response, csv_response, json_response, text_response
from .report import dashboard, text_report
from .routes import route_table
from .simulate import run_simulation
from .system import system_payload
from .version import version_payload

if TYPE_CHECKING:  # pragma: no cover - imported only for type checkers
    from .server import Server


def health(server: "Server", request: Request) -> Response:
    store = server.runtime.store
    return json_response(
        {"status": "ok", "kinds": store.kinds(), "records": store.file_count(), "detail": store.describe()}
    )


def health_checks(server: "Server", request: Request) -> Response:
    return json_response({"checks": checks.run_checks(server.runtime)})


def snapshot(server: "Server", request: Request) -> Response:
    return json_response(snapshot_module.collect(server.runtime))


def describe(server: "Server", request: Request) -> Response:
    return json_response(describe_module.collect(server.runtime))


def report(server: "Server", request: Request) -> Response:
    return json_response(dashboard(server.runtime))


def report_text(server: "Server", request: Request) -> Response:
    return text_response(text_report(server.runtime))


def chain(server: "Server", request: Request) -> Response:
    line = control_chain()
    last_stage = line.last()
    return json_response(
        {
            "name": line.name,
            "steps": [step.as_dict() for step in line.steps()],
            "ordered": line.before(Stage.ACCESS, Stage.FIRE),
            "last_stage": "" if last_stage is None else last_stage.value,
        }
    )


def catalog(server: "Server", request: Request) -> Response:
    return json_response(
        {"routes": [route.as_dict() for route in route_table()], "paths": server.router.paths()}
    )


def ops_counters(server: "Server", request: Request) -> Response:
    return json_response(ops.collect(server.runtime).as_dict())


def ops_reset_store(server: "Server", request: Request) -> Response:
    return json_response(ops.reset_store(server.runtime))


def telemetry(server: "Server", request: Request) -> Response:
    return json_response(telemetry_module.collect(server.runtime).as_dict())


def version(server: "Server", request: Request) -> Response:
    return json_response(version_payload())


def system(server: "Server", request: Request) -> Response:
    return json_response(system_payload())


def metrics(server: "Server", request: Request) -> Response:
    return json_response(server.metrics.snapshot().as_dict())


def metrics_reset(server: "Server", request: Request) -> Response:
    server.metrics.reset()
    return json_response({"reset": True})


def namespaces_list(server: "Server", request: Request) -> Response:
    registry = server.runtime.namespaces
    return json_response({"items": [item.as_dict() for item in registry.list()], "names": registry.names()})


def namespace_ops(server: "Server", request: Request) -> Response:
    registry = server.runtime.namespaces
    segment_id = request.str_field("id")
    if not segment_id:
        raise RequestError(400, "id is required")
    if registry.exists(segment_id):
        registry.remove(segment_id)
        return json_response({"removed": True, "count": registry.count()})
    registry.add(segment_id, request.str_field("name"))
    return json_response({"added": True, "count": registry.count()})


def cabins_list(server: "Server", request: Request) -> Response:
    return json_response({"items": [cabin.as_dict() for cabin in server.runtime.cabins.all()]})


def cabin_status(server: "Server", request: Request) -> Response:
    return json_response(status_summary(server.runtime.cabins).as_dict())


def cabin_extend(server: "Server", request: Request) -> Response:
    registry = server.runtime.cabins
    cabin = Cabin(
        id=request.str_field("id"),
        name=request.str_field("name"),
        zone=request.str_field("zone"),
        partition=request.str_field("partition"),
    )
    registry.extend(cabin)
    return json_response({"items": [item.as_dict() for item in registry.all()]})


def cabin_zones(server: "Server", request: Request) -> Response:
    registry = server.runtime.cabins
    zone = request.query_field("zone")
    cabin_id = request.query_field("id")
    payload: dict[str, object] = {"zones": registry.zones()}
    if zone:
        payload["cabins"] = [cabin.as_dict() for cabin in registry.zone_cabins(zone)]
    if cabin_id:
        state = registry.find(cabin_id)
        payload["found"] = registry.has(cabin_id)
        payload["cabin"] = None if state is None else state.as_dict()
    return json_response(payload)


def cabin_rename(server: "Server", request: Request) -> Response:
    registry = server.runtime.cabins
    registry.rename(request.str_field("id"), request.str_field("name"))
    state = registry.find(request.str_field("id"))
    return json_response({"cabin": None if state is None else state.as_dict()})


def cabin_move(server: "Server", request: Request) -> Response:
    registry = server.runtime.cabins
    registry.move(request.str_field("id"), request.str_field("zone"), request.str_field("partition"))
    state = registry.find(request.str_field("id"))
    return json_response({"cabin": None if state is None else state.as_dict()})


def cabin_zone(server: "Server", request: Request) -> Response:
    registry = server.runtime.cabins
    registry.assign_zone(request.str_field("id"), request.str_field("zone"))
    state = registry.find(request.str_field("id"))
    return json_response({"cabin": None if state is None else state.as_dict()})


def cabin_door(server: "Server", request: Request) -> Response:
    registry = server.runtime.cabins
    cabin_id = request.str_field("id")
    if request.payload.get("release", True):
        registry.release_door(cabin_id)
    else:
        registry.seal_door(cabin_id)
    return json_response({"id": cabin_id, "open": registry.door_state(cabin_id)})


def cabin_valve(server: "Server", request: Request) -> Response:
    registry = server.runtime.cabins
    cabin_id = request.str_field("id")
    if request.payload.get("open", True):
        registry.open_valve(cabin_id)
    else:
        registry.close_valve(cabin_id)
    return json_response({"id": cabin_id, "open": registry.valve_state(cabin_id)})


def cabin_label(server: "Server", request: Request) -> Response:
    labels = server.runtime.cabins.label(request.str_field("id"), request.str_field("label"))
    return json_response({"id": request.str_field("id"), "labels": labels})


def cabin_retire(server: "Server", request: Request) -> Response:
    registry = server.runtime.cabins
    registry.retire(request.str_field("id"))
    return json_response({"items": [cabin.as_dict() for cabin in registry.all()]})


def access_requests(server: "Server", request: Request) -> Response:
    access = server.runtime.access
    return json_response(
        {"pending": [item.payload() for item in access.pending()], "count": access.request_count()}
    )


def access_summary(server: "Server", request: Request) -> Response:
    return json_response(server.runtime.access.summary())


def access_approvals(server: "Server", request: Request) -> Response:
    access = server.runtime.access
    items = []
    for approval in list_approvals(server.runtime.store):
        payload = approval.as_dict()
        payload["durable"] = access.approved(approval.id)
        items.append(payload)
    return json_response({"items": items})


def access_submit(server: "Server", request: Request) -> Response:
    item = server.runtime.access.submit(request.str_field("cabin_id"), request.str_field("requester"))
    return json_response(item.payload())


def access_grant(server: "Server", request: Request) -> Response:
    approval = server.runtime.access.grant(request.str_field("request_id"))
    record_event(server.runtime, "access_granted", approval.id)
    return json_response(approval.as_dict())


def access_approve(server: "Server", request: Request) -> Response:
    approval = server.runtime.access.approve(request.str_field("cabin_id"), request.str_field("requester"))
    record_event(server.runtime, "access_approved", approval.id)
    return json_response(approval.as_dict())


def access_revoke(server: "Server", request: Request) -> Response:
    approval_id = request.str_field("approval_id")
    if not revoke_approval(server.runtime.store, approval_id):
        raise RequestError(404, "approval not found")
    return json_response({"revoked": approval_id})


def env_overview(server: "Server", request: Request) -> Response:
    env = server.runtime.env
    return json_response(
        {
            "alarms": [alarm.as_dict() for alarm in env.alarms()],
            "sensor_1_cabin": env.route_alarm("sensor-1"),
            "threshold": env.threshold(),
        }
    )


def env_sensors(server: "Server", request: Request) -> Response:
    env = server.runtime.env
    sensor = request.query_field("sensor")
    payload: dict[str, object] = {"sensors": env.sensors(), "snapshot": env_snapshot(env)}
    if sensor:
        payload["history"] = [reading.as_dict() for reading in env.history(sensor)]
    return json_response(payload)


def env_trend(server: "Server", request: Request) -> Response:
    sensor = request.query_field("sensor")
    if not sensor:
        raise RequestError(400, "sensor is required")
    env = server.runtime.env
    return json_response(
        {
            "sensor": sensor,
            "count": env.reading_count(sensor),
            "average": env.average(sensor),
            "max": env.maximum(sensor),
            "min": env.minimum(sensor),
            "trend": env.trend(sensor),
            "variance": env.variance(sensor),
            "stddev": env.std_dev(sensor),
            "percentile": env.percentile(sensor, 0.95),
        }
    )


def env_threshold(server: "Server", request: Request) -> Response:
    return json_response({"threshold": server.runtime.env.threshold()})


def env_filter(server: "Server", request: Request) -> Response:
    moving = server.runtime.filter
    return json_response({"size": moving.size(), "window": moving.window(), "describe": moving.describe()})


def env_sample(server: "Server", request: Request) -> Response:
    alarmed = server.runtime.env.sample(
        request.str_field("sensor_id"), request.float_field("value"), request.str_field("at")
    )
    return json_response({"alarmed": alarmed})


def env_partition(server: "Server", request: Request) -> Response:
    revision = server.runtime.env.repartition(
        request.str_field("partition_id"), request.str_field("cabin_id")
    )
    return json_response(
        {"revision": revision, "sensor_1_cabin": server.runtime.env.route_alarm("sensor-1")}
    )


def env_threshold_set(server: "Server", request: Request) -> Response:
    value = request.float_field("threshold")
    return json_response({"threshold": server.runtime.env.set_threshold(value)})


def env_filter_reset(server: "Server", request: Request) -> Response:
    server.runtime.filter.reset()
    return json_response({"reset": True, "window": server.runtime.filter.window()})


def drain_pits(server: "Server", request: Request) -> Response:
    drain = server.runtime.drain
    pit_id = request.query_field("id")
    payload: dict[str, object] = {
        "items": [pit.as_dict() for pit in drain.pits()],
        "count": drain.pit_count(),
        "levels": drain.levels(),
    }
    if pit_id:
        payload["is_high"] = drain.is_high(pit_id)
    return json_response(payload)


def drain_high(server: "Server", request: Request) -> Response:
    drain = server.runtime.drain
    return json_response(
        {
            "high": [pit.as_dict() for pit in drain.high_pits()],
            "cycles": [cycle.as_dict() for cycle in drain.cycles()],
            "count": drain.cycle_count(),
        }
    )


def drain_state(server: "Server", request: Request) -> Response:
    drain = server.runtime.drain
    pump = server.runtime.pump
    return json_response(
        {
            "state": drain.state_name(),
            "runs": drain.run_count(),
            "pump_runs": pump.runs(),
            "pump": pump.describe(),
        }
    )


def drain_pending(server: "Server", request: Request) -> Response:
    return json_response({"pending": server.runtime.drain.pending_commands()})


def drain_pit(server: "Server", request: Request) -> Response:
    drain = server.runtime.drain
    drain.add_pit(
        Pit(
            id=request.str_field("id"),
            cabin=request.str_field("cabin"),
            water_level=request.float_field("water_level"),
        )
    )
    return json_response({"items": [pit.as_dict() for pit in drain.pits()]})


def drain_levels(server: "Server", request: Request) -> Response:
    raw = request.payload.get("levels", {})
    if not isinstance(raw, dict):
        raise RequestError(400, "field levels must be an object")
    levels = {str(key): float(value) for key, value in raw.items()}
    return json_response({"levels": server.runtime.drain.set_levels(levels)})


def drain_start(server: "Server", request: Request) -> Response:
    drain = server.runtime.drain
    drain.start_drain(request.str_field("cabin_id"))
    return json_response({"runs": drain.run_count(), "state": drain.state_name()})


def drain_execute(server: "Server", request: Request) -> Response:
    drain = server.runtime.drain
    executed = drain.execute(request.str_field("command_key"))
    return json_response({"executed": executed, "runs": drain.run_count()})


def drain_threshold(server: "Server", request: Request) -> Response:
    drain = server.runtime.drain
    drain.set_threshold(request.float_field("threshold"))
    return json_response({"threshold": drain.threshold(), "high": [pit.as_dict() for pit in drain.high_pits()]})


def drain_cycle(server: "Server", request: Request) -> Response:
    drain = server.runtime.drain
    drain.record_cycle(request.str_field("cabin_id"), request.str_field("at"))
    return json_response({"count": drain.cycle_count()})


def drain_state_set(server: "Server", request: Request) -> Response:
    return json_response({"state": server.runtime.drain.set_state(request.str_field("state", "idle"))})


def drain_in_flight(server: "Server", request: Request) -> Response:
    drain = server.runtime.drain
    command_key = request.str_field("command_key")
    if not command_key:
        raise RequestError(400, "command_key is required")
    if request.str_field("action") == "clear":
        drain.clear_in_flight(command_key)
    else:
        drain.mark_in_flight(command_key)
    return json_response(
        {"in_flight": drain.in_flight(command_key), "pending": drain.pending_commands()}
    )


def vent_state(server: "Server", request: Request) -> Response:
    return json_response(server.runtime.ventilation.state().as_dict())


def vent_toggle(server: "Server", request: Request) -> Response:
    running = server.runtime.ventilation.toggle()
    return json_response({"running": running, "status": server.runtime.ventilation.status()})


def vent_start(server: "Server", request: Request) -> Response:
    running = server.runtime.ventilation.start()
    return json_response({"running": running, "status": server.runtime.ventilation.status()})


def fire_detectors(server: "Server", request: Request) -> Response:
    fire = server.runtime.fire
    return json_response(
        {"items": [detector.as_dict() for detector in fire.detectors()], "count": fire.detector_count()}
    )


def fire_zones(server: "Server", request: Request) -> Response:
    fire = server.runtime.fire
    return json_response({"zones": fire.zones(), "count": fire.zone_count()})


def fire_alarms(server: "Server", request: Request) -> Response:
    fire = server.runtime.fire
    return json_response(
        {"items": fire.alarms(), "count": fire.alarm_count(), "distinct": fire.distinct_alarms()}
    )


def fire_report(server: "Server", request: Request) -> Response:
    return json_response(server.runtime.fire.report().as_dict())


def fire_detector_add(server: "Server", request: Request) -> Response:
    fire = server.runtime.fire
    detector = fire.add_detector(Detector(id=request.str_field("id"), zone=request.str_field("zone")))
    return json_response({"detector": detector.as_dict()})


def fire_detect(server: "Server", request: Request) -> Response:
    fire = server.runtime.fire
    detector_id = request.str_field("detector_id")
    zone = fire.zone_of_detector(detector_id)
    if not zone:
        raise RequestError(404, f"detector {detector_id} not found")
    fire.detect(detector_id)
    record_event(server.runtime, "fire_detect", detector_id)
    return json_response({"detector": detector_id, "zone": zone})


def fire_smoke(server: "Server", request: Request) -> Response:
    zone = request.str_field("zone")
    server.runtime.fire.handle_smoke(zone)
    record_event(server.runtime, "fire_smoke", zone)
    return json_response({"ok": True, "zone": zone})


def fire_zone(server: "Server", request: Request) -> Response:
    zone = request.str_field("zone")
    order = server.runtime.fire.activate_zones(zone)
    record_event(server.runtime, "fire_zone", zone)
    return json_response({"order": order})


def fire_alarm(server: "Server", request: Request) -> Response:
    raised = server.runtime.fire.raise_alarm(request.str_field("event_key"))
    return json_response({"raised": raised, "count": server.runtime.fire.alarm_count()})


def fire_clear(server: "Server", request: Request) -> Response:
    return json_response({"count": server.runtime.fire.clear_alarms()})


def patrols(server: "Server", request: Request) -> Response:
    patrol = server.runtime.patrol
    return json_response(
        {
            "route": [checkpoint.as_dict() for checkpoint in patrol.route()],
            "missed": patrol.missed(),
            "route_ids": patrol.route_ids(),
            "checked_ids": patrol.checked_cabin_ids(),
            "sorted_route": patrol.sorted_route(),
            "checkins": [check.as_dict() for check in patrol.check_ins()],
        }
    )


def patrol_progress(server: "Server", request: Request) -> Response:
    patrol = server.runtime.patrol
    now = request.query_field("now")
    return json_response({"progress": patrol.progress(), "overdue": patrol.overdue(now)})


def patrol_summary(server: "Server", request: Request) -> Response:
    return json_response(server.runtime.patrol.summary().as_dict())


def patrol_checkin(server: "Server", request: Request) -> Response:
    patrol = server.runtime.patrol
    patrol.check_in(request.str_field("cabin_id"), request.str_field("at"))
    return json_response({"missed": patrol.missed()})


def patrol_rebuild(server: "Server", request: Request) -> Response:
    route = server.runtime.patrol.rebuild_route()
    return json_response({"route": [checkpoint.as_dict() for checkpoint in route]})


def patrol_due(server: "Server", request: Request) -> Response:
    patrol = server.runtime.patrol
    return json_response({"due": patrol.set_due(request.str_field("cabin_id"), request.str_field("due"))})


def cables_list(server: "Server", request: Request) -> Response:
    cables = server.runtime.cables
    return json_response(
        {"items": [cable.as_dict() for cable in cables.list()], "summary": cables.summary().as_dict()}
    )


def cable_stats(server: "Server", request: Request) -> Response:
    cables = server.runtime.cables
    cable_id = request.query_field("id")
    if not cable_id:
        raise RequestError(400, "id is required")
    threshold = request.query_field("threshold")
    limit = float(threshold) if threshold else 75.0
    return json_response(
        {
            "id": cable_id,
            "history": cables.history(cable_id),
            "average": cables.average(cable_id),
            "max": cables.maximum(cable_id),
            "min": cables.minimum(cable_id),
            "over": cables.over(cable_id, limit),
        }
    )


def cable_health(server: "Server", request: Request) -> Response:
    cables = server.runtime.cables
    return json_response(
        {
            "danger": danger_count(cables, 75.0),
            "healthy": healthy_count(cables, 75.0),
            "load_factor": load_factor(cables),
        }
    )


def cable_temp(server: "Server", request: Request) -> Response:
    cables = server.runtime.cables
    cable_id = request.str_field("cable_id")
    if not cables.set_temp(cable_id, request.float_field("temp")):
        raise RequestError(404, "cable not found")
    state = cables.get(cable_id)
    return json_response({} if state is None else state.as_dict())


def cable_sample(server: "Server", request: Request) -> Response:
    cables = server.runtime.cables
    cable_id = request.str_field("cable_id")
    if not cables.sample(cable_id, request.float_field("value")):
        raise RequestError(404, "cable not found")
    return json_response({"history": cables.history(cable_id)})


def quota_report(server: "Server", request: Request) -> Response:
    quota = server.runtime.quota
    return json_response(
        {
            "items": [baseline.as_dict() for baseline in quota.load_report()],
            "book_cabins": server.runtime.power_book.cabins(),
        }
    )


def quota_usage(server: "Server", request: Request) -> Response:
    quota = server.runtime.quota
    return json_response(
        {
            "usage": quota.usage_by_cabin(),
            "total": quota.total_usage(),
            "peak": quota.peak(),
            "ranked": quota.ranked_cabin_ids(),
            "baseline": [baseline.as_dict() for baseline in quota.load_report()],
        }
    )


def quota_advanced(server: "Server", request: Request) -> Response:
    quota = server.runtime.quota
    cabin_id = request.query_field("cabin")
    factor = request.query_field("factor")
    scale = float(factor) if factor else 1.0
    return json_response(
        {
            "allocated": quota.allocated(),
            "overage_total": quota.overage_total(),
            "peak_cabin": quota.peak_cabin(),
            "forecast": quota.forecast(cabin_id, scale),
            "overage": quota.overage(cabin_id),
        }
    )


def quota_summary(server: "Server", request: Request) -> Response:
    return json_response(server.runtime.quota.report().as_dict())


def quota_refresh(server: "Server", request: Request) -> Response:
    items = server.runtime.quota.refresh_baseline(request.float_field("watts"))
    return json_response({"items": [baseline.as_dict() for baseline in items]})


def quota_add_usage(server: "Server", request: Request) -> Response:
    usage = server.runtime.quota.add_usage(
        request.str_field("cabin_id"), request.float_field("watts")
    )
    return json_response({"usage": usage})


def _optional_float(request: Request, field: str) -> float | None:
    """Read a float field where ``None`` (absent/null) means "use the clock"."""

    value = request.payload.get(field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RequestError(400, f"{field} must be a number")
    return float(value)


def link_report(server: "Server", request: Request) -> Response:
    return json_response(server.runtime.link_monitor.report().as_dict())


def link_links(server: "Server", request: Request) -> Response:
    link_id = request.query_field("id")
    monitor = server.runtime.link_monitor
    if link_id:
        link = monitor.find_link(link_id)
        if link is None:
            raise RequestError(404, "link not found")
        return json_response(monitor.link_view(link_id).as_dict())
    return json_response({"items": [link.as_dict() for link in monitor.links()]})


def link_devices(server: "Server", request: Request) -> Response:
    monitor = server.runtime.link_monitor
    devices = monitor.devices(request.query_field("link"))
    return json_response(
        {
            "items": [device.as_dict() for device in devices],
            "count": len(devices),
            "summary": summarize_devices(devices).as_dict(),
        }
    )


def link_events(server: "Server", request: Request) -> Response:
    monitor = server.runtime.link_monitor
    events = monitor.events()
    scope = request.query_field("scope")
    if scope:
        events = [event for event in events if event.scope == scope]
    return json_response({"items": [event.as_dict() for event in events], "count": len(events)})


def link_linkages(server: "Server", request: Request) -> Response:
    gate = server.runtime.link_monitor.gate()
    key = request.query_field("key")
    payload: dict[str, object] = {
        "items": [item.as_dict() for item in gate.all()],
        "paused": [item.key for item in gate.paused_linkages()],
        "mode": server.runtime.link_monitor.mode(),
    }
    if key:
        payload["allowed"] = gate.is_allowed(key)
    return json_response(payload)


def link_register(server: "Server", request: Request) -> Response:
    monitor = server.runtime.link_monitor
    link_id = request.str_field("link_id")
    if link_id:
        link = monitor.register_link(
            link_id,
            name=request.str_field("name"),
            interval=request.float_field("interval", DEFAULT_LINK_INTERVAL),
            miss_threshold=request.int_field("miss_threshold", DEFAULT_LINK_MISS),
            recover_hits=request.int_field("recover_hits", DEFAULT_LINK_HITS),
            offline_ratio=request.float_field("offline_ratio", DEFAULT_LINK_RATIO),
            recover_stable_seconds=request.float_field("stable_seconds", DEFAULT_LINK_STABLE),
        )
        return json_response({"link": link.as_dict()})
    device_id = request.str_field("device_id")
    if not device_id:
        raise RequestError(400, "link_id or device_id is required")
    parent = request.str_field("on", "link-main")
    device = monitor.register_device(device_id, parent)
    return json_response({"device": device.as_dict()})


def link_heartbeat(server: "Server", request: Request) -> Response:
    monitor = server.runtime.link_monitor
    device_id = request.str_field("device_id")
    at = _optional_float(request, "at")
    if device_id not in {d.id for d in monitor.devices()}:
        raise RequestError(404, f"device {device_id} not registered")
    device = monitor.heartbeat(device_id, at)
    return json_response({"device": device.as_dict(), "mode": monitor.mode()})


def link_sweep(server: "Server", request: Request) -> Response:
    monitor = server.runtime.link_monitor
    at = _optional_float(request, "at")
    transitions = monitor.sweep(at)
    return json_response(
        {
            "transitions": [event.as_dict() for event in transitions],
            "mode": monitor.mode(),
            "paused": [item.key for item in monitor.gate().paused_linkages()],
        }
    )


def link_confirm(server: "Server", request: Request) -> Response:
    monitor = server.runtime.link_monitor
    link_id = request.str_field("link_id", "link-main")
    force = bool(request.payload.get("force", False))
    try:
        result = monitor.confirm_recovery(link_id, force=force)
    except ValueError as exc:
        raise RequestError(409, str(exc))
    record_event(server.runtime, "link_recovery_confirmed", link_id)
    return json_response(result.as_dict())


def link_rollback(server: "Server", request: Request) -> Response:
    monitor = server.runtime.link_monitor
    link_id = request.str_field("link_id", "link-main")
    try:
        result = monitor.rollback_recovery(link_id)
    except ValueError as exc:
        raise RequestError(404, str(exc))
    record_event(server.runtime, "link_recovery_rolled_back", link_id)
    return json_response(result.as_dict())


def audit_list(server: "Server", request: Request) -> Response:
    registry = server.runtime.audit
    return json_response({"items": [record.as_dict() for record in registry.list()], "count": registry.count()})


def audit_query(server: "Server", request: Request) -> Response:
    registry = server.runtime.audit
    records = registry.records()
    payload: dict[str, object] = {
        "count": registry.count(),
        "events": events(records),
        "latest_id": registry.latest_id(),
    }
    event = request.query_field("event")
    if event:
        payload["filtered"] = [record.as_dict() for record in filter_records(records, event)]
    last_value = request.query_field("last")
    if last_value.isdigit():
        payload["last"] = [record.as_dict() for record in last(records, int(last_value))]
    recent = most_recent(records)
    if recent is not None:
        payload["most_recent"] = recent.as_dict()
    start = request.query_field("start")
    end = request.query_field("end")
    if start and end:
        payload["between"] = [record.as_dict() for record in between(records, start, end)]
    return json_response(payload)


def audit_summary(server: "Server", request: Request) -> Response:
    registry = server.runtime.audit
    return json_response({"summary": registry.summary(), "by_event": registry.count_by_event()})


def audit_marks(server: "Server", request: Request) -> Response:
    marks = server.runtime.dedupe
    payload: dict[str, object] = {"count": marks.count(), "describe": marks.describe()}
    key = request.query_field("key")
    if key:
        payload["has"] = marks.has(key)
    return json_response(payload)


def audit_append(server: "Server", request: Request) -> Response:
    event = request.str_field("event")
    if not event:
        raise RequestError(400, "event is required")
    entries = record_event(server.runtime, event, request.str_field("ref"))
    return json_response({"appended": [entry.as_dict() for entry in entries]})


def audit_clear(server: "Server", request: Request) -> Response:
    return json_response({"count": server.runtime.audit.clear()})


def audit_marks_clear(server: "Server", request: Request) -> Response:
    server.runtime.dedupe.clear()
    return json_response({"cleared": True, "count": server.runtime.dedupe.count()})


def suppression_list(server: "Server", request: Request) -> Response:
    log = server.runtime.suppression
    return json_response(
        {
            "zones": log.zones(),
            "distinct": log.distinct_zones(),
            "entries": [entry.as_dict() for entry in log.entries()],
            "count": log.count(),
            "describe": log.describe(),
        }
    )


def suppression_clear(server: "Server", request: Request) -> Response:
    server.runtime.suppression.clear()
    return json_response({"cleared": True, "count": server.runtime.suppression.count()})


def store_stats(server: "Server", request: Request) -> Response:
    store = server.runtime.store
    summary = summarize(store)
    return json_response(
        {"root": store.root, "kinds": store.kinds(), "stats": store.stats(), "summary": summary.as_dict()}
    )


def store_export(server: "Server", request: Request) -> Response:
    store = server.runtime.store
    kind = request.query_field("kind")
    if kind:
        return json_response({"kind": kind, "items": export_kind(store, kind)})
    return json_response({"items": export_all(store)})


def cycle(server: "Server", request: Request) -> Response:
    return run_cycle(server.runtime, request)


def simulate(server: "Server", request: Request) -> Response:
    return run_simulation(server.runtime, request)


def export_audit(server: "Server", request: Request) -> Response:
    columns, rows = audit_export(server.runtime.audit)
    return csv_response(to_csv(columns, rows))


def export_telemetry(server: "Server", request: Request) -> Response:
    columns, rows = telemetry_export(telemetry_module.collect(server.runtime))
    return csv_response(to_csv(columns, rows))


def export_store(server: "Server", request: Request) -> Response:
    columns, rows = store_csv_export(server.runtime.store)
    return csv_response(to_csv(columns, rows))
