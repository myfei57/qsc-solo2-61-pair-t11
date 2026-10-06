"""Background heartbeat-timeout scanner.

Heartbeats arrive as explicit POSTs, but silence is detected by the clock:
this daemon wakes every ``interval`` seconds and runs one link evaluation
per registered link so OFFLINE verdicts and degraded transitions happen even
when no operator request is in flight.
"""

from __future__ import annotations

import threading
from typing import Callable

from .monitor import LinkMonitor


class LinkScanner:
    def __init__(
        self,
        monitor: LinkMonitor,
        interval: float = 5.0,
        *,
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        self._monitor = monitor
        self._interval = interval
        self._on_error = on_error
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> "LinkScanner":
        if self._thread is not None:
            return self
        self._thread = threading.Thread(target=self._run, name="link-scanner", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float | None = None) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    def scan_once(self) -> None:
        for config in self._monitor._repo.list_link_configs():  # noqa: SLF001 - own subsystem
            self._monitor.tick(config.id)

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.scan_once()
            except Exception as exc:  # noqa: BLE001 - scanner must survive a bad tick
                if self._on_error is not None:
                    self._on_error(exc)
