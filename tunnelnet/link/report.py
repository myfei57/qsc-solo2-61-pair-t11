"""Read projections over link and device state, shared by every console view."""

from __future__ import annotations

from dataclasses import dataclass

from .model import Device, DeviceState, Link, Linkage, LinkState


@dataclass(frozen=True)
class DeviceSummary:
    total: int
    online: int
    jitter: int
    offline: int
    unknown: int

    def as_dict(self) -> dict[str, int]:
        return {
            "total": self.total,
            "online": self.online,
            "jitter": self.jitter,
            "offline": self.offline,
            "unknown": self.unknown,
        }


@dataclass(frozen=True)
class LinkView:
    link: Link
    devices: tuple[Device, ...]
    summary: DeviceSummary

    def as_dict(self) -> dict[str, object]:
        return {
            "link": self.link.as_dict(),
            "devices": [device.as_dict() for device in self.devices],
            "summary": self.summary.as_dict(),
        }


@dataclass(frozen=True)
class LinkReport:
    mode: str
    links: tuple[LinkView, ...]
    devices: DeviceSummary
    paused: tuple[Linkage, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "links": [view.as_dict() for view in self.links],
            "devices": self.devices.as_dict(),
            "paused": [linkage.as_dict() for linkage in self.paused],
        }


def summarize_devices(devices: list[Device]) -> DeviceSummary:
    counts = {state: 0 for state in DeviceState}
    for device in devices:
        counts[device.state] += 1
    return DeviceSummary(
        total=len(devices),
        online=counts[DeviceState.ONLINE],
        jitter=counts[DeviceState.JITTER],
        offline=counts[DeviceState.OFFLINE],
        unknown=counts[DeviceState.UNKNOWN],
    )


def is_degraded(links: list[Link]) -> bool:
    """System runs degraded while any link is degraded or recovering."""

    return any(link.state is not LinkState.ONLINE for link in links)


def system_mode(links: list[Link]) -> str:
    if any(link.state is LinkState.DEGRADED for link in links):
        return LinkState.DEGRADED.value
    if any(link.state is LinkState.RECOVERING for link in links):
        return LinkState.RECOVERING.value
    return LinkState.ONLINE.value
