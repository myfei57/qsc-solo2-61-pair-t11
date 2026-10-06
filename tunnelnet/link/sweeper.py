"""Background sweeper that ages heartbeats on a fixed interval."""

from __future__ import annotations

import threading

from .model import DEFAULT_SWEEP_SECONDS


class Sweeper:
    """Runs ``LinkMonitor.sweep`` periodically on a daemon thread.

    The monitor serializes every decision behind its own lock, so a sweep can
    run concurrently with heartbeat ingestion and operator confirmations.
    """

    def __init__(self, monitor, interval: float = DEFAULT_SWEEP_SECONDS) -> None:
        self._monitor = monitor
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> "Sweeper":
        if self._thread is not None:
            return self
        self._thread = threading.Thread(target=self._run, name="link-sweeper", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval)
            self._thread = None

    def run_once(self) -> int:
        """Sweep immediately, returning how many transitions were emitted."""

        return len(self._monitor.sweep())

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._monitor.sweep()
            except Exception:  # noqa: BLE001 - the loop must survive a bad tick
                continue
