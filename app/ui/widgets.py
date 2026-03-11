from __future__ import annotations

from PySide6.QtCore import QRect, QSize, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QLayout,
    QLayoutItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class FlowLayout(QLayout):
    """Layout that arranges widgets left-to-right and wraps to the next row."""

    def __init__(self, parent: QWidget | None = None, spacing: int = 4) -> None:
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self.setSpacing(spacing)

    def addItem(self, item: QLayoutItem) -> None:  # noqa: N802
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> QLayoutItem | None:  # noqa: N802
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int) -> QLayoutItem | None:  # noqa: N802
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QSize:  # noqa: N802
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _do_layout(self, rect: QRect, *, test_only: bool) -> int:
        m = self.contentsMargins()
        effective = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x = effective.x()
        y = effective.y()
        line_height = 0
        sp = self.spacing()

        for item in self._items:
            w = item.sizeHint().width()
            h = item.sizeHint().height()
            if x + w > effective.right() and line_height > 0:
                x = effective.x()
                y += line_height + sp
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(x, y, w, h))
            x += w + sp
            line_height = max(line_height, h)

        return y + line_height - rect.y() + m.bottom()


class ChipSelector(QWidget):
    """Dropdown combo + removable chip tags for multi-selection."""

    selection_changed = Signal()

    _CHIP_STYLE = (
        "QPushButton { background: #3a7bd5; color: white; border: none; "
        "border-radius: 10px; padding: 2px 8px; font-size: 12px; }"
        "QPushButton:hover { background: #c0392b; }"
    )

    def __init__(self, placeholder: str = "Select...", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._options: list[str] = []
        self._selected: list[str] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(2)

        self._combo = QComboBox()
        self._combo.setPlaceholderText(placeholder)
        self._combo.setCurrentIndex(-1)
        self._combo.activated.connect(self._on_picked)  # type: ignore[arg-type]
        root.addWidget(self._combo)

        self._chips_widget = QWidget()
        self._chips_layout = FlowLayout(self._chips_widget, spacing=4)
        self._chips_layout.setContentsMargins(0, 2, 0, 2)
        root.addWidget(self._chips_widget)

    # ── public API ─────────────────────────────────────────────

    def set_options(self, options: list[str]) -> None:
        """Replace available options; selections not in *options* are dropped."""
        self._options = list(options)
        valid = set(options)
        before = list(self._selected)
        self._selected = [s for s in self._selected if s in valid]
        self._sync_ui()
        if self._selected != before:
            self.selection_changed.emit()

    def selected(self) -> list[str]:
        return list(self._selected)

    def set_selected(self, items: list[str]) -> None:
        valid = set(self._options)
        self._selected = [s for s in items if s in valid]
        self._sync_ui()

    def clear_all(self) -> None:
        self._options.clear()
        self._selected.clear()
        self._sync_ui()

    # ── internal ───────────────────────────────────────────────

    def _on_picked(self, _index: int) -> None:
        text = self._combo.currentText()
        if text and text not in self._selected:
            self._selected.append(text)
            self._sync_ui()
            self.selection_changed.emit()
        self._combo.setCurrentIndex(-1)

    def _remove_chip(self, text: str) -> None:
        if text in self._selected:
            self._selected.remove(text)
            self._sync_ui()
            self.selection_changed.emit()

    def _sync_ui(self) -> None:
        self._combo.blockSignals(True)
        self._combo.clear()
        selected_set = set(self._selected)
        for opt in self._options:
            if opt not in selected_set:
                self._combo.addItem(opt)
        self._combo.setCurrentIndex(-1)
        self._combo.blockSignals(False)

        while self._chips_layout.count():
            item = self._chips_layout.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()

        for text in self._selected:
            chip = QPushButton(f"{text}  \u00d7")
            chip.setStyleSheet(self._CHIP_STYLE)
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.setFixedHeight(22)
            chip.clicked.connect(  # type: ignore[arg-type]
                lambda _checked=False, t=text: self._remove_chip(t)
            )
            self._chips_layout.addWidget(chip)

        self._chips_widget.updateGeometry()
