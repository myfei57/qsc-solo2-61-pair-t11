"""The gate that suspends and resumes linkages around a degraded episode.

Safety linkages (fire suppression, damper close) are never paused: when fresh
sensor data cannot be trusted the protective action stays armed. Only ordinary
automatic linkages are suspended, and their suspension is reported explicitly
so the operator knows exactly which联动 stopped.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Protocol

from .model import Linkage, LinkageClass


class LinkageController(Protocol):
    """Drives one real linkage; failures must raise so the caller can roll back."""

    def pause(self, key: str) -> None:  # pragma: no cover - structural
        ...

    def resume(self, key: str) -> None:  # pragma: no cover - structural
        ...


DEFAULT_LINKAGES: tuple[Linkage, ...] = (
    Linkage(key="access.release", name="release cabin door", stage="access"),
    Linkage(key="env.alarm", name="route environment alarm", stage="environment"),
    Linkage(key="vent.run", name="run ventilation bank", stage="ventilation"),
    Linkage(key="drain.pump", name="start the sump pump", stage="drain"),
    Linkage(
        key="fire.suppression",
        name="activate fire suppression",
        stage="fire",
        linkage_class=LinkageClass.SAFETY,
    ),
    Linkage(key="patrol.checkin", name="patrol check-in scheduling", stage="patrol"),
    Linkage(key="energy.meter", name="power metering linkage", stage="energy"),
    Linkage(
        key="audit.record",
        name="linkage audit recording",
        stage="audit",
        linkage_class=LinkageClass.SAFETY,
    ),
)


def _noop(_key: str) -> None:
    return None


class LinkageGate:
    """Holds the paused flag of every linkage and drives the real controllers."""

    def __init__(
        self,
        linkages: tuple[Linkage, ...] = DEFAULT_LINKAGES,
        controller: LinkageController | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._linkages = {item.key: item for item in linkages}
        self._pause: Callable[[str], None] = _noop
        self._resume: Callable[[str], None] = _noop
        if controller is not None:
            self._pause = controller.pause
            self._resume = controller.resume

    def keys(self) -> list[str]:
        with self._lock:
            return sorted(self._linkages)

    def all(self) -> list[Linkage]:
        with self._lock:
            return [self._linkages[key] for key in sorted(self._linkages)]

    def pausable(self) -> list[str]:
        with self._lock:
            return sorted(
                key
                for key, item in self._linkages.items()
                if item.linkage_class is LinkageClass.NORMAL
            )

    def is_paused(self, key: str) -> bool:
        with self._lock:
            item = self._linkages.get(key)
            return item is not None and item.paused

    def is_allowed(self, key: str) -> bool:
        """Safety linkages always run; a paused normal linkage is blocked."""

        with self._lock:
            item = self._linkages.get(key)
            if item is None:
                return True
            return item.linkage_class is LinkageClass.SAFETY or not item.paused

    def paused_linkages(self) -> list[Linkage]:
        with self._lock:
            return [self._linkages[key] for key in sorted(self._linkages) if self._linkages[key].paused]

    def pause_normal(self) -> list[str]:
        """Pause every normal linkage, returning the keys that changed (idempotent)."""

        changed: list[str] = []
        with self._lock:
            for key in sorted(self._linkages):
                item = self._linkages[key]
                if item.linkage_class is LinkageClass.NORMAL and not item.paused:
                    self._pause(key)
                    self._linkages[key] = Linkage(
                        key=item.key,
                        name=item.name,
                        stage=item.stage,
                        linkage_class=item.linkage_class,
                        paused=True,
                    )
                    changed.append(key)
        return changed

    def resume_normal(self) -> list[str]:
        """Resume every normal linkage, returning the keys that changed (idempotent)."""

        changed: list[str] = []
        with self._lock:
            for key in sorted(self._linkages):
                item = self._linkages[key]
                if item.linkage_class is LinkageClass.NORMAL and item.paused:
                    self._resume(key)
                    self._linkages[key] = Linkage(
                        key=item.key,
                        name=item.name,
                        stage=item.stage,
                        linkage_class=item.linkage_class,
                        paused=False,
                    )
                    changed.append(key)
        return changed

    def describe(self) -> str:
        paused = len(self.paused_linkages())
        return f"linkages={len(self._linkages)} paused={paused}"
