"""Run long jobs off the UI thread, with progress and cancel."""

from __future__ import annotations

import logging
import threading
import traceback

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from ..log import logger
from ..progress import Cancelled, Reporter


class _Signals(QObject):
    progress = Signal(str, object)        # message, fraction | None
    done = Signal(object)
    failed = Signal(str, str)             # friendly message, traceback
    cancelled = Signal()
    finished = Signal()


class Job(QRunnable):
    """``fn(reporter)`` on a pool thread. Exceptions the user can act on
    (network, bad input) arrive as ``failed(message, "")``; anything else
    carries its traceback for the log."""

    def __init__(self, fn, label: str = ""):
        super().__init__()
        self.fn, self.label = fn, label
        self.signals = _Signals()
        self._cancel = threading.Event()
        self.setAutoDelete(True)

    def cancel(self):
        self._cancel.set()

    def run(self):
        reporter = Reporter(lambda msg, frac: self.signals.progress.emit(msg, frac),
                            self._cancel.is_set)
        try:
            result = self.fn(reporter)
        except Cancelled:
            logger.info("%s cancelled", self.label or "job")
            self.signals.cancelled.emit()
        except (ValueError, OSError, RuntimeError) as exc:
            logger.error("%s failed: %s", self.label or "job", exc)
            self.signals.failed.emit(str(exc), "")
        except Exception as exc:  # noqa: BLE001
            tb = traceback.format_exc()
            logger.error("%s crashed:\n%s", self.label or "job", tb)
            self.signals.failed.emit(f"Unexpected error: {exc}", tb)
        else:
            self.signals.done.emit(result)
        finally:
            self.signals.finished.emit()


def start(job: Job) -> Job:
    QThreadPool.globalInstance().start(job)
    return job


class _LogEmitter(QObject):
    record = Signal(str, str)             # level name, formatted line


class QtLogHandler(logging.Handler):
    """Forwards log records to the UI thread (connect to ``.emitter.record``)."""

    def __init__(self):
        super().__init__(level=logging.INFO)
        self.emitter = _LogEmitter()
        self.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S"))

    def emit(self, rec):
        try:
            self.emitter.record.emit(rec.levelname, self.format(rec))
        except Exception:  # noqa: BLE001
            pass
