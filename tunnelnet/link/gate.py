"""Gate that consults the link monitor before a linkage may act."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .model import MODE_NORMAL

if TYPE_CHECKING:  # pragma: no cover - imported only for type checkers
    from .monitor import LinkMonitor


class LinkageGate:
    """Decides whether an automatic linkage is allowed to run right now.

    Only linkages whose persisted gate is ``active`` may run.  Fail-safe
    linkages are always permitted: losing data must never silence a hazard
    response.  Every refusal is returned as an explanation instead of an
    exception so callers can report "paused because ..." in their payload.
    """

    def __init__(self, monitor: "LinkMonitor") -> None:
        self._monitor = monitor

    def is_active(self, linkage_id: str) -> bool:
        state = self._monitor._repo.load_linkage_state(linkage_id)  # noqa: SLF001 - same subsystem
        return state is None or state.actual == "active"

    def gate(self, linkage_id: str) -> tuple[bool, str]:
        state = self._monitor._repo.load_linkage_state(linkage_id)  # noqa: SLF001
        if state is None:
            # Ungated linkage: existing control flows stay untouched.
            return True, ""
        if state.actual == "active":
            return True, ""
        return False, f"linkage {linkage_id} is {state.actual}; command paused by link protection"

    def normal_links(self) -> list[str]:
        return [
            link.id
            for link in self._monitor._repo.list_link_states()  # noqa: SLF001
            if link.mode == MODE_NORMAL
        ]
