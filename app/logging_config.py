from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, Signal


class LogSignalEmitter(QObject):
    new_log = Signal(str)


class _QSignalHandler(logging.Handler):
    def __init__(self, emitter: LogSignalEmitter) -> None:
        super().__init__()
        self._emitter = emitter

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._emitter.new_log.emit(self.format(record))
        except RuntimeError:
            pass


def setup_logging() -> LogSignalEmitter:
    logs_dir = Path(__file__).resolve().parent.parent / "logs"
    logs_dir.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_file = logs_dir / f"lumina_{timestamp}.log"

    emitter = LogSignalEmitter()

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    signal_handler = _QSignalHandler(emitter)

    fmt = logging.Formatter("[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s")
    file_handler.setFormatter(fmt)
    signal_handler.setFormatter(fmt)

    root = logging.getLogger("lumina")
    root.setLevel(logging.DEBUG)
    root.addHandler(file_handler)
    root.addHandler(signal_handler)

    return emitter
