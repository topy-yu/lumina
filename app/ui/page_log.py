from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.logging_config import LogSignalEmitter

_LOGS_DIR = Path(__file__).resolve().parent.parent.parent / "logs"
_MAX_LINES = 50_000
_FLUSH_INTERVAL_MS = 100


class LogPage(QWidget):
    def __init__(self, emitter: LogSignalEmitter, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._emitter = emitter
        self._all_lines: list[str] = []
        self._filter_level = "ALL"
        self._pending: list[str] = []

        self._log_view = QPlainTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self._log_view.setMaximumBlockCount(_MAX_LINES)
        self._log_view.setStyleSheet("font-family: Consolas, 'Courier New', monospace; font-size: 12px;")

        self._flush_timer = QTimer(self)
        self._flush_timer.setInterval(_FLUSH_INTERVAL_MS)
        self._flush_timer.timeout.connect(self._flush_pending)  # type: ignore[arg-type]

        self._level_combo = QComboBox()
        self._level_combo.addItems(["ALL", "DEBUG", "INFO", "WARNING", "ERROR"])
        self._level_combo.currentTextChanged.connect(self._on_filter_changed)  # type: ignore[arg-type]

        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self._clear_display)  # type: ignore[arg-type]

        open_folder_btn = QPushButton("Open Log Folder")
        open_folder_btn.clicked.connect(self._open_log_folder)  # type: ignore[arg-type]

        self._auto_scroll = True

        toolbar = QHBoxLayout()
        toolbar.addWidget(self._level_combo)
        toolbar.addWidget(clear_btn)
        toolbar.addWidget(open_folder_btn)
        toolbar.addStretch()

        layout = QVBoxLayout(self)
        layout.addLayout(toolbar)
        layout.addWidget(self._log_view, stretch=1)

        self._emitter.new_log.connect(self._on_new_log)  # type: ignore[arg-type]

    def _on_new_log(self, message: str) -> None:
        self._all_lines.append(message)
        if len(self._all_lines) > _MAX_LINES:
            self._all_lines = self._all_lines[-_MAX_LINES:]
        if self._passes_filter(message):
            self._pending.append(message)
            if not self._flush_timer.isActive():
                self._flush_timer.start()

    def _flush_pending(self) -> None:
        self._flush_timer.stop()
        if not self._pending:
            return
        self._log_view.setUpdatesEnabled(False)
        for line in self._pending:
            self._log_view.appendPlainText(line)
        self._pending.clear()
        self._log_view.setUpdatesEnabled(True)
        if self._auto_scroll:
            scrollbar = self._log_view.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())

    def _passes_filter(self, message: str) -> bool:
        if self._filter_level == "ALL":
            return True
        return f"] [{self._filter_level}]" in message

    def _on_filter_changed(self, level: str) -> None:
        self._filter_level = level
        self._refilter()

    def _refilter(self) -> None:
        filtered = [line for line in self._all_lines if self._passes_filter(line)]
        self._log_view.setPlainText("\n".join(filtered))
        if self._auto_scroll:
            scrollbar = self._log_view.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())

    def _clear_display(self) -> None:
        self._all_lines.clear()
        self._pending.clear()
        self._log_view.clear()

    def _open_log_folder(self) -> None:
        _LOGS_DIR.mkdir(exist_ok=True)
        path = str(_LOGS_DIR)
        if sys.platform == "win32":
            os.startfile(path)  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])  # noqa: S603, S607
        else:
            subprocess.Popen(["xdg-open", path])  # noqa: S603, S607
