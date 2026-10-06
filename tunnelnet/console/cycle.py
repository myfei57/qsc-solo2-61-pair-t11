"""One full tunnel control cycle walked in chain order.

When the link is degraded the ordinary linkages are paused by the link gate and
skipped here; safety linkages (fire suppression, audit) always run. The
response lists exactly which linkages were skipped so degradation is explicit.
"""

from __future__ import annotations

from tunnelnet.ns import Stage, control_chain

from .audit_log import record_event
from .http import Request, Response, json_response
from .runtime import Runtime


def run_cycle(rt: Runtime, request: Request) -> Response:
    """Walk the whole chain once, skipping linkages paused by degraded mode."""

    cabin_id = request.str_field("cabin_id")
    requester = request.str_field("requester", "operator")
    zone = request.str_field("zone")
    sensor_id = request.str_field("sensor_id", "sensor-1")
    samples = request.float_list("samples")
    fallback = request.float_field("value")
    at = request.str_field("at")
    watts = request.float_field("watts")

    gate = rt.link_monitor
    skipped: list[str] = []

    def allowed(key: str) -> bool:
        if gate.allowed(key):
            return True
        skipped.append(key)
        return False

    reading = sum(samples) / len(samples) if samples else fallback
    approval = None
    if allowed("access.release"):
        approval = rt.access.approve(cabin_id, requester)
        record_event(rt, "access_approved", approval.id)

    alarmed = False
    if allowed("env.alarm"):
        alarmed = rt.env.sample(sensor_id, reading, at)

    # Fire suppression is a safety linkage: it always runs, even degraded.
    zones = rt.fire.activate_zones(zone)
    rt.fire.handle_smoke(zone)
    raised = rt.fire.raise_alarm(f"{zone}:{at}")

    if allowed("vent.run"):
        rt.ventilation.start()

    if allowed("patrol.checkin"):
        rt.patrol.check_in(cabin_id, at)

    usage = 0.0
    if allowed("energy.meter"):
        usage = rt.quota.add_usage(cabin_id, watts)

    if allowed("drain.pump"):
        rt.drain.start_drain(cabin_id)
        rt.drain.record_cycle(cabin_id, at)

    # Audit is a safety linkage: every cycle is recorded, degraded or not.
    record_event(rt, "cycle", cabin_id)
    line = control_chain()

    return json_response(
        {
            "approval": None if approval is None else approval.id,
            "alarmed": alarmed,
            "reading": reading,
            "zones": zones,
            "alarm_raised": raised,
            "patrol_route": rt.patrol.route_len(),
            "usage": usage,
            "drain_runs": rt.drain.run_count(),
            "audit_count": rt.audit.count(),
            "link_mode": gate.mode(),
            "skipped_linkages": skipped,
            "last_stage": Stage.AUDIT.value if line.contains(Stage.AUDIT) else "",
        }
    )
