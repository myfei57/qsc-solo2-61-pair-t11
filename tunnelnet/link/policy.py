"""Pure decision functions for device status and link availability.

Keeping the rules free of persistence and I/O makes the chosen trade-off
explicit and testable:

* a single missed heartbeat is not an OFFLINE verdict - the device becomes
  SUSPECT immediately, and only ``miss_limit`` consecutive misses confirm
  OFFLINE (lower false-positive rate at the price of a bounded detection
  delay);
* repeated healthy/missing oscillations inside ``flap_window`` are promoted
  to JITTER, which is treated as unavailable even when a heartbeat happens to
  have just arrived, because data from a flapping link is not trustworthy;
* SUSPECT devices stay counted as *available* (with a visible warning) so a
  single delayed heartbeat cannot trigger degradation, but critical devices
  that reach OFFLINE/JITTER fail the whole link regardless of the ratio.
"""

from __future__ import annotations

from collections.abc import Sequence

from .model import (
    JITTER,
    LINK_DOWN,
    LINK_UP,
    OFFLINE,
    ONLINE,
    SUSPECT,
    DeviceConfig,
    DeviceState,
    LinkConfig,
)

# Event kinds emitted while evaluating one device.
EV_FIRST_CONTACT = "device_first_contact"
EV_HEARTBEAT = "device_heartbeat"
EV_MISSED = "device_heartbeat_missed"
EV_SUSPECT = "device_suspect"
EV_OFFLINE = "device_offline"
EV_ONLINE = "device_online"
EV_JITTER = "device_jitter"
EV_JITTER_CLEAR = "device_jitter_cleared"


def evaluate_device(
    state: DeviceState,
    config: LinkConfig,
    now: int,
    *,
    heartbeat_seq: int | None = None,
) -> tuple[DeviceState, list[tuple[str, str, dict[str, object]]]]:
    """Return the next device state and the edge events that occurred.

    ``heartbeat_seq`` is the sequence number of a heartbeat arriving at
    ``now``; ``None`` means a periodic timeout evaluation with no heartbeat.
    A stale or duplicate sequence number is ignored.
    """

    events: list[tuple[str, str, dict[str, object]]] = []
    next_state = DeviceState.from_dict(state.as_dict())

    if heartbeat_seq is not None:
        if heartbeat_seq <= state.last_seq:
            # Duplicate or replayed packet: it proves nothing new.
            return state, events
        next_state.last_seq = heartbeat_seq
        next_state.last_heartbeat_at = now
        next_state.missed = 0
    elif state.last_heartbeat_at is not None:
        next_state.missed = max(0, (now - state.last_heartbeat_at) // config.heartbeat_period)
    # A device that has never been heard keeps the bootstrap missed count.

    first_contact = state.last_seq == -1 and heartbeat_seq is not None
    recovering = state.status in (OFFLINE, JITTER) and heartbeat_seq is not None

    # 1. Underlying verdict before the jitter overlay is applied.
    if first_contact:
        base = SUSPECT
        events.append((EV_FIRST_CONTACT, f"{state.id} first heartbeat received", {"seq": heartbeat_seq}))
    elif recovering:
        base = SUSPECT
        events.append((EV_SUSPECT, f"{state.id} reachable again, awaiting confirmation", {"seq": heartbeat_seq}))
    elif next_state.missed >= config.miss_limit:
        base = OFFLINE
    elif next_state.missed >= 1:
        base = SUSPECT
    elif state.status == SUSPECT:
        base = ONLINE if heartbeat_seq is not None else SUSPECT
    else:
        base = ONLINE

    # 2. Rolling window of healthy -> missing edges for the jitter detector.
    if next_state.missed >= 1 and state.missed == 0 and not (state.last_seq == -1):
        next_state.flap_at.append(now)
        next_state.last_drop_at = now
        events.append(
            (
                EV_MISSED,
                f"{state.id} missed heartbeat ({next_state.missed}/{config.miss_limit})",
                {"missed": next_state.missed, "limit": config.miss_limit},
            )
        )
    next_state.flap_at = [stamp for stamp in next_state.flap_at if stamp > now - config.flap_window]
    hot = len(next_state.flap_at) >= config.flap_limit

    # 3. Enter or exit the jitter overlay.  Exit needs one full quiet window
    #    after the most recent drop while the device is currently healthy.
    last_drop = next_state.last_drop_at
    quiet_for = now - last_drop if last_drop is not None else config.flap_window
    if not state.flapping and hot:
        next_state.flapping = True
        events.append(
            (
                EV_JITTER,
                f"{state.id} flapping: {len(next_state.flap_at)} drops within {config.flap_window}s",
                {"drops": len(next_state.flap_at), "window": config.flap_window},
            )
        )
    elif state.flapping and not hot and next_state.missed == 0 and quiet_for >= config.flap_window:
        next_state.flapping = False
        next_state.flap_at = []
        next_state.last_drop_at = None
        events.append((EV_JITTER_CLEAR, f"{state.id} link stable for {quiet_for}s", {}))

    # 4. Mask the underlying verdict while the overlay is active.
    observed = JITTER if next_state.flapping else base

    # 5. Edge events for the publicly observed status.
    if observed == OFFLINE and state.status != OFFLINE:
        events.append(
            (
                EV_OFFLINE,
                f"{state.id} offline after {next_state.missed} missed heartbeats",
                {"missed": next_state.missed, "limit": config.miss_limit},
            )
        )
    if observed == ONLINE and state.status != ONLINE:
        events.append((EV_ONLINE, f"{state.id} online", {"seq": next_state.last_seq}))
    if observed == SUSPECT and state.status == ONLINE:
        events.append(
            (
                EV_SUSPECT,
                f"{state.id} heartbeat overdue ({next_state.missed}/{config.miss_limit})",
                {"missed": next_state.missed, "limit": config.miss_limit},
            )
        )
    if observed == ONLINE and state.status == ONLINE and heartbeat_seq is not None:
        events.append((EV_HEARTBEAT, f"{state.id} heartbeat {heartbeat_seq}", {"seq": heartbeat_seq}))

    next_state.status = observed
    next_state.updated_at = now
    next_state.note = _note(next_state, config)
    if observed != state.status or next_state.missed != state.missed or heartbeat_seq is not None:
        next_state.version = state.version + 1
    next_state.entered_at = state.entered_at if observed == state.status else now
    next_state.stable_since = now if observed in (SUSPECT, ONLINE) else None
    return next_state, events


def _note(state: DeviceState, config: LinkConfig) -> str:
    if state.status == ONLINE:
        return f"last heartbeat seq={state.last_seq}"
    if state.status == JITTER:
        return f"unstable: {len(state.flap_at)} drops within {config.flap_window}s"
    if state.status == OFFLINE:
        return f"{state.missed} consecutive heartbeats lost (limit {config.miss_limit})"
    if state.missed == 0:
        return "seen, awaiting a confirming heartbeat"
    return f"heartbeat overdue {state.missed}/{config.miss_limit}"


def link_availability(
    config: LinkConfig,
    devices: Sequence[tuple[DeviceConfig, DeviceState]],
) -> tuple[str, list[str], float, list[str]]:
    """Decide whether a link carries trustworthy data.

    Returns ``(availability, unavailable_ids, ratio, critical_lost)``.
    SUSPECT is deliberately not counted as unavailable; JITTER and OFFLINE
    are.  A lost critical device fails the link on its own.  A link with no
    registered devices cannot be proven healthy and is reported down.
    """

    unavailable = [state.id for _, state in devices if state.status in (OFFLINE, JITTER)]
    critical_lost = [device.id for device, state in devices if device.critical and state.id in unavailable]
    total = len(devices)
    ratio = len(unavailable) / total if total else 1.0
    if total == 0 or critical_lost or ratio >= config.offline_ratio:
        return LINK_DOWN, sorted(unavailable), ratio, sorted(critical_lost)
    return LINK_UP, sorted(unavailable), ratio, []
