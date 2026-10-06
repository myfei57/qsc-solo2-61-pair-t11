"""Durable link and device state written behind the control service.

State is persisted *before* a control action runs, mirroring the approval
pattern elsewhere in the service, so a crash between "decided offline" and
"paused the linkage" resumes with the decision still on disk.
"""

from __future__ import annotations

from tunnelnet.store import Store

from .model import Device, Link, Transition

LINK_KIND = "link"
DEVICE_KIND = "device"
EVENT_KIND = "link_event"


def save_link(store: Store, link: Link) -> None:
    store.save(LINK_KIND, link.id, link.as_dict())


def load_link(store: Store, link_id: str) -> Link | None:
    payload = store.load(LINK_KIND, link_id)
    return None if payload is None else Link.from_dict(payload)


def list_links(store: Store) -> list[Link]:
    links = [link for link_id in store.list(LINK_KIND) if (link := load_link(store, link_id))]
    return sorted(links, key=lambda link: link.id)


def save_device(store: Store, device: Device) -> None:
    store.save(DEVICE_KIND, device.id, device.as_dict())


def load_device(store: Store, device_id: str) -> Device | None:
    payload = store.load(DEVICE_KIND, device_id)
    return None if payload is None else Device.from_dict(payload)


def list_devices(store: Store) -> list[Device]:
    devices = [
        device
        for device_id in store.list(DEVICE_KIND)
        if (device := load_device(store, device_id))
    ]
    return sorted(devices, key=lambda device: device.id)


def save_event(store: Store, event: Transition) -> None:
    """Append one transition, keyed by its monotonic sequence so replay orders it."""

    store.save(EVENT_KIND, f"{event.seq:012d}", event.as_dict())


def load_event(store: Store, seq: int) -> Transition | None:
    payload = store.load(EVENT_KIND, f"{seq:012d}")
    if payload is None:
        return None
    return transition_from_dict(payload)


def list_events(store: Store) -> list[Transition]:
    events: list[Transition] = []
    for record_id in store.list(EVENT_KIND):
        payload = store.load(EVENT_KIND, record_id)
        if payload is not None:
            events.append(transition_from_dict(payload))
    return sorted(events, key=lambda event: event.seq)


def event_count(store: Store) -> int:
    return len(store.list(EVENT_KIND))


def transition_from_dict(payload: dict[str, object]) -> Transition:
    return Transition(
        scope=str(payload.get("scope", "")),
        target_id=str(payload.get("target_id", "")),
        old=str(payload.get("old", "")),
        new=str(payload.get("new", "")),
        at=float(payload.get("at", 0.0)),
        episode=int(payload.get("episode", 0)),
        reason=str(payload.get("reason", "")),
        seq=int(payload.get("seq", 0)),
    )
