"""One full tunnel control cycle walked in chain order."""

from __future__ import annotations

from tunnelnet.ns import Stage, control_chain

from .audit_log import record_event
from .http import Request, Response, json_response
from .runtime import Runtime

# Linkages whose physical actions are withheld while the collection link is
# degraded.  The fire alarm is fail-safe and is deliberately absent.
GATED_DRAIN = "drain-auto"
GATED_VENT = "vent-auto"


def run_cycle(rt: Runtime, request: Request) -> Response:
    """Walk the whole chain once, in production order."""

    cabin_id = request.str_field("cabin_id")
    requester = request.str_field("requester", "operator")
    zone = request.str_field("zone")
    sensor_id = request.str_field("sensor_id", "sensor-1")
    samples = request.float_list("samples")
    fallback = request.float_field("value")
    at = request.str_field("at")
    watts = request.float_field("watts")

    reading = sum(samples) / len(samples) if samples else fallback

    approval = rt.access.approve(cabin_id, requester)
    record_event(rt, "access_approved", approval.id)

    alarmed = rt.env.sample(sensor_id, reading, at)

    zones = rt.fire.activate_zones(zone)
    rt.fire.handle_smoke(zone)
    raised = rt.fire.raise_alarm(f"{zone}:{at}")

    rt.patrol.check_in(cabin_id, at)

    usage = rt.quota.add_usage(cabin_id, watts)

    # Data-driven linkages only act when the link that feeds them is trusted;
    # in degraded mode they are skipped and the skip is reported explicitly.
    paused: list[str] = []

    def gated(linkage_id: str) -> bool:
        allowed, reason = rt.link_gate.gate(linkage_id)
        if not allowed:
            paused.append(linkage_id)
        return allowed

    drain_runs_before = rt.drain.run_count()
    if gated(GATED_DRAIN):
        rt.drain.start_drain(cabin_id)
        rt.drain.record_cycle(cabin_id, at)
    drain_runs = rt.drain.run_count()

    if gated(GATED_VENT) and not rt.ventilation.running():
        rt.ventilation.start()

    record_event(rt, "cycle", cabin_id)
    line = control_chain()

    return json_response(
        {
            "approval": approval.id,
            "alarmed": alarmed,
            "reading": reading,
            "zones": zones,
            "alarm_raised": raised,
            "patrol_route": rt.patrol.route_len(),
            "usage": usage,
            "drain_runs": drain_runs,
            "drain_started": drain_runs > drain_runs_before,
            "paused_linkages": paused,
            "degraded": bool(paused),
            "audit_count": rt.audit.count(),
            "last_stage": Stage.AUDIT.value if line.contains(Stage.AUDIT) else "",
        }
    )
