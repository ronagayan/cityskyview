"""Progress reporting + cooperative cancellation shared by every long task."""

from __future__ import annotations

from typing import Callable, Optional

from .log import logger


class Cancelled(Exception):
    """Raised inside a worker when the user pressed Cancel."""


class Reporter:
    """Passed down through fetch / build / export.

    ``on_progress(message, fraction)`` -- fraction is 0..1 or None (unknown).
    ``is_cancelled()`` -- polled at safe points via :meth:`check`.
    """

    def __init__(self, on_progress: Optional[Callable[[str, Optional[float]], None]] = None,
                 is_cancelled: Optional[Callable[[], bool]] = None):
        self._on_progress = on_progress
        self._is_cancelled = is_cancelled
        self._lo, self._hi = 0.0, 1.0

    def span(self, lo: float, hi: float) -> "Reporter":
        """A child reporter whose 0..1 maps onto lo..hi of this one."""
        child = Reporter(self._on_progress, self._is_cancelled)
        child._lo = self._lo + (self._hi - self._lo) * lo
        child._hi = self._lo + (self._hi - self._lo) * hi
        return child

    def step(self, message: str, fraction: Optional[float] = None, log: bool = True):
        self.check()
        if log:
            logger.info(message)
        if self._on_progress is not None:
            f = None if fraction is None else (
                self._lo + (self._hi - self._lo) * max(0.0, min(1.0, fraction)))
            self._on_progress(message, f)

    def check(self):
        if self._is_cancelled is not None and self._is_cancelled():
            raise Cancelled()


NULL_REPORTER = Reporter()
