from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.logging_config import LogSignalEmitter

_LOGS_DIR = Path(__file__).resolve().parent.parent.parent / "logs"


class LogPage(QWidget):
    def __init__(self, emitter: LogSignalEmitter, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._emitter = emitter
        self._all_lines: list[str] = []
        self._filter_level = "ALL"

        self._log_view = QTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self._log_view.setStyleSheet("font-family: Consolas, 'Courier New', monospace; font-size: 12px;")

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
        if self._passes_filter(message):
            self._log_view.append(message)
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
        self._log_view.clear()
        for line in self._all_lines:
            if self._passes_filter(line):
                self._log_view.append(line)
        if self._auto_scroll:
            scrollbar = self._log_view.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())

    def _clear_display(self) -> None:
        self._all_lines.clear()
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
