from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from pathlib import Path

from PIL import Image
from PySide6.QtCore import QMimeData, QRect, QSize, QUrl, Qt, Signal
from PySide6.QtGui import QGuiApplication, QPixmap, QTransform
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLayoutItem,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.db.repository import PhotoRecord, PhotoRepository
from app.services.config_service import ConfigService


# ── Helper: flow layout ───────────────────────────────────────


class _FlowLayout(QLayout):
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


# ── Helper: chip-based multi-selector ─────────────────────────


class _ChipSelector(QWidget):
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
        self._chips_layout = _FlowLayout(self._chips_widget, spacing=4)
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
            chip = QPushButton(f"{text}  ×")
            chip.setStyleSheet(self._CHIP_STYLE)
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.setFixedHeight(22)
            chip.clicked.connect(  # type: ignore[arg-type]
                lambda _checked=False, t=text: self._remove_chip(t)
            )
            self._chips_layout.addWidget(chip)

        self._chips_widget.updateGeometry()


# ── Search page ───────────────────────────────────────────────


class SearchPage(QWidget):
    def __init__(
        self,
        config_service: ConfigService,
        repository: PhotoRepository,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._config_service = config_service
        self._repository = repository
        self._library_root: Path | None = None
        self._db_path: Path | None = None
        self._results: list[PhotoRecord] = []
        self._current_index: int = -1
        self._zoom_factor: float = 1.0
        self._rotation_angle: int = 0
        self._current_pixmap: QPixmap | None = None

        self._build_ui()

    # ── UI construction ────────────────────────────────────────

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        # --- Folder selection ---
        folder_row = QHBoxLayout()

        level1_col = QVBoxLayout()
        level1_col.addWidget(QLabel("First-level directory:"))
        self._level1_selector = _ChipSelector("Select first-level...")
        self._level1_selector.selection_changed.connect(self._on_level1_changed)  # type: ignore[arg-type]
        level1_col.addWidget(self._level1_selector)

        level2_col = QVBoxLayout()
        level2_col.addWidget(QLabel("Second-level directory:"))
        self._level2_selector = _ChipSelector("Select second-level...")
        level2_col.addWidget(self._level2_selector)

        folder_row.addLayout(level1_col)
        folder_row.addLayout(level2_col)
        layout.addLayout(folder_row)

        # --- Tag input + Search ---
        search_row = QHBoxLayout()
        search_row.addWidget(QLabel("Tags:"))
        self._tags_input = QLineEdit()
        self._tags_input.setPlaceholderText("tag1, tag2, ... (matches tags & autotags)")
        self._tags_input.returnPressed.connect(self._run_search)  # type: ignore[arg-type]
        search_row.addWidget(self._tags_input)
        self._search_btn = QPushButton("Search")
        self._search_btn.clicked.connect(self._run_search)  # type: ignore[arg-type]
        search_row.addWidget(self._search_btn)
        layout.addLayout(search_row)

        # --- Display area ---
        self._image_label = QLabel("No photo to display")
        self._image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image_label.setStyleSheet("background-color: #222; color: #aaa;")

        self._scroll_area = QScrollArea()
        self._scroll_area.setWidget(self._image_label)
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setMinimumHeight(300)
        layout.addWidget(self._scroll_area, stretch=3)

        # --- Navigation + action buttons ---
        nav_row = QHBoxLayout()
        self._prev_btn = QPushButton("← Previous")
        self._prev_btn.clicked.connect(self._go_previous)  # type: ignore[arg-type]
        self._next_btn = QPushButton("Next →")
        self._next_btn.clicked.connect(self._go_next)  # type: ignore[arg-type]
        self._zoom_in_btn = QPushButton("Zoom +")
        self._zoom_in_btn.clicked.connect(self._zoom_in)  # type: ignore[arg-type]
        self._zoom_out_btn = QPushButton("Zoom −")
        self._zoom_out_btn.clicked.connect(self._zoom_out)  # type: ignore[arg-type]
        self._zoom_reset_btn = QPushButton("Fit")
        self._zoom_reset_btn.clicked.connect(self._zoom_reset)  # type: ignore[arg-type]
        self._rotate_left_btn = QPushButton("↺ Rotate L")
        self._rotate_left_btn.clicked.connect(self._rotate_left)  # type: ignore[arg-type]
        self._rotate_right_btn = QPushButton("↻ Rotate R")
        self._rotate_right_btn.clicked.connect(self._rotate_right)  # type: ignore[arg-type]
        self._save_btn = QPushButton("Save")
        self._save_btn.clicked.connect(self._save_current)  # type: ignore[arg-type]
        self._copy_btn = QPushButton("Copy")
        self._copy_btn.clicked.connect(self._copy_current)  # type: ignore[arg-type]
        self._delete_btn = QPushButton("Delete")
        self._delete_btn.clicked.connect(self._delete_current)  # type: ignore[arg-type]
        self._index_label = QLabel("No results")

        nav_row.addWidget(self._prev_btn)
        nav_row.addWidget(self._next_btn)
        nav_row.addWidget(self._zoom_in_btn)
        nav_row.addWidget(self._zoom_out_btn)
        nav_row.addWidget(self._zoom_reset_btn)
        nav_row.addWidget(self._rotate_left_btn)
        nav_row.addWidget(self._rotate_right_btn)
        nav_row.addWidget(self._save_btn)
        nav_row.addWidget(self._copy_btn)
        nav_row.addWidget(self._delete_btn)
        nav_row.addStretch()
        nav_row.addWidget(self._index_label)
        layout.addLayout(nav_row)

        # --- Results table ---
        self._results_table = QTableWidget(0, 6)
        self._results_table.setHorizontalHeaderLabels(
            ["Path", "Capture Time", "Tags", "Auto Tags", "MD5", "Actions"]
        )
        self._results_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._results_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._results_table.verticalHeader().setVisible(False)
        self._results_table.horizontalHeader().setStretchLastSection(True)
        self._results_table.setColumnWidth(0, 220)
        self._results_table.setColumnWidth(1, 150)
        self._results_table.setColumnWidth(2, 150)
        self._results_table.setColumnWidth(3, 180)
        self._results_table.setColumnWidth(4, 100)
        self._results_table.setColumnWidth(5, 80)
        layout.addWidget(self._results_table, stretch=2)

        self._update_nav_state()

    # ── Page activation ────────────────────────────────────────

    def refresh(self) -> None:
        """Reload folder lists when navigating to this page."""
        config = self._config_service.load()
        errors = self._config_service.validate(config)
        if errors:
            self._level1_selector.clear_all()
            self._level2_selector.clear_all()
            self._library_root = None
            self._db_path = None
            return

        self._library_root = Path(config.library_root)
        self._db_path = Path(config.db_path)

        if self._library_root.exists() and self._library_root.is_dir():
            dirs = sorted(d.name for d in self._library_root.iterdir() if d.is_dir())
            self._level1_selector.set_options(dirs)
        else:
            self._level1_selector.clear_all()

    # ── Folder selection ───────────────────────────────────────

    def _on_level1_changed(self) -> None:
        if self._library_root is None:
            return

        selected_l1 = self._level1_selector.selected()
        new_options: list[str] = []
        for l1 in sorted(selected_l1):
            l1_path = self._library_root / l1
            if l1_path.is_dir():
                for d in sorted(l1_path.iterdir(), key=lambda p: p.name):
                    if d.is_dir():
                        new_options.append(f"{l1}/{d.name}")

        self._level2_selector.set_options(new_options)

    # ── Search ─────────────────────────────────────────────────

    def _run_search(self) -> None:
        if self._library_root is None or self._db_path is None:
            QMessageBox.warning(self, "Not ready", "Configure library settings first.")
            return
        if not self._db_path.exists():
            QMessageBox.information(self, "No database", "Database not found. Import photos first.")
            return

        selected_level1 = self._level1_selector.selected()
        selected_level2 = self._level2_selector.selected()

        allowed_prefixes: list[str] = []
        if selected_level2:
            for path in selected_level2:
                allowed_prefixes.append(f"{path}/")
        elif selected_level1:
            for l1 in selected_level1:
                allowed_prefixes.append(f"{l1}/")

        search_tags = self._parse_tags(self._tags_input.text())

        all_photos = self._repository.get_all_photos(self._db_path)

        if allowed_prefixes:
            all_photos = [
                p for p in all_photos
                if any(
                    p.relative_path.replace("\\", "/").startswith(prefix)
                    for prefix in allowed_prefixes
                )
            ]

        if search_tags:
            all_photos = self._filter_by_tags(all_photos, search_tags)

        self._results = all_photos
        self._current_index = 0 if all_photos else -1
        self._zoom_factor = 1.0

        self._populate_results_table()
        self._display_current()
        self._update_nav_state()

    @staticmethod
    def _parse_tags(text: str) -> list[str]:
        tags: list[str] = []
        seen: set[str] = set()
        for tag in text.split(","):
            clean = tag.strip()
            if not clean or clean.lower() in seen:
                continue
            seen.add(clean.lower())
            tags.append(clean)
        return tags

    @staticmethod
    def _load_json_tags(raw: str | None) -> list[str]:
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, list) else []
        except (json.JSONDecodeError, TypeError):
            return []

    @classmethod
    def _filter_by_tags(cls, photos: list[PhotoRecord], search_tags: list[str]) -> list[PhotoRecord]:
        lower_search = {t.lower() for t in search_tags}
        filtered: list[PhotoRecord] = []
        for photo in photos:
            all_tags = {
                t.lower()
                for t in cls._load_json_tags(photo.tags) + cls._load_json_tags(photo.autotags)
            }
            if lower_search & all_tags:
                filtered.append(photo)
        return filtered

    # ── Results table ──────────────────────────────────────────

    def _populate_results_table(self) -> None:
        self._results_table.setRowCount(len(self._results))
        for row, photo in enumerate(self._results):
            path_item = QTableWidgetItem(photo.relative_path)
            path_item.setToolTip(photo.relative_path)
            self._results_table.setItem(row, 0, path_item)
            self._results_table.setItem(row, 1, QTableWidgetItem(photo.capture_time or "-"))

            tags = self._load_json_tags(photo.tags)
            autotags = self._load_json_tags(photo.autotags)

            tags_item = QTableWidgetItem(", ".join(tags) if tags else "-")
            tags_item.setToolTip(", ".join(tags) if tags else "")
            autotags_item = QTableWidgetItem(", ".join(autotags) if autotags else "-")
            autotags_item.setToolTip(", ".join(autotags) if autotags else "")

            self._results_table.setItem(row, 2, tags_item)
            self._results_table.setItem(row, 3, autotags_item)

            md5_item = QTableWidgetItem(photo.md5[:8] + "…")
            md5_item.setToolTip(photo.md5)
            self._results_table.setItem(row, 4, md5_item)

            actions = QWidget()
            actions_layout = QHBoxLayout(actions)
            actions_layout.setContentsMargins(0, 0, 0, 0)
            preview_btn = QPushButton("Preview")
            preview_btn.clicked.connect(  # type: ignore[arg-type]
                lambda _checked=False, r=row: self._preview_row(r)
            )
            actions_layout.addWidget(preview_btn)
            self._results_table.setCellWidget(row, 5, actions)

    # ── Display area ───────────────────────────────────────────

    def _preview_row(self, row: int) -> None:
        if 0 <= row < len(self._results):
            self._current_index = row
            self._zoom_factor = 1.0
            self._display_current()
            self._update_nav_state()

    def _display_current(self) -> None:
        if self._current_index < 0 or self._current_index >= len(self._results):
            self._image_label.clear()
            self._image_label.setText("No photo to display")
            self._current_pixmap = None
            return

        assert self._library_root is not None
        photo = self._results[self._current_index]
        image_path = self._library_root / photo.relative_path

        if not image_path.exists():
            self._image_label.clear()
            self._image_label.setText(f"File not found: {photo.relative_path}")
            self._current_pixmap = None
            return

        pixmap = QPixmap(str(image_path))
        if pixmap.isNull():
            self._image_label.setText(f"Cannot load: {photo.relative_path}")
            self._current_pixmap = None
            return

        self._current_pixmap = pixmap
        self._rotation_angle = 0
        self._apply_zoom()

    def _apply_zoom(self) -> None:
        if self._current_pixmap is None:
            return

        pixmap = self._current_pixmap
        if self._rotation_angle % 360 != 0:
            pixmap = pixmap.transformed(
                QTransform().rotate(self._rotation_angle),
                Qt.TransformationMode.SmoothTransformation,
            )

        available = self._scroll_area.viewport().size()
        base_w = max(available.width() - 4, 100)
        base_h = max(available.height() - 4, 100)

        scaled = pixmap.scaled(
            int(base_w * self._zoom_factor),
            int(base_h * self._zoom_factor),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._image_label.setPixmap(scaled)
        self._image_label.adjustSize()

    # ── Navigation ─────────────────────────────────────────────

    def _update_nav_state(self) -> None:
        has_current = 0 <= self._current_index < len(self._results)

        self._prev_btn.setEnabled(has_current and self._current_index > 0)
        self._next_btn.setEnabled(has_current and self._current_index < len(self._results) - 1)
        self._zoom_in_btn.setEnabled(has_current)
        self._zoom_out_btn.setEnabled(has_current)
        self._zoom_reset_btn.setEnabled(has_current)
        self._rotate_left_btn.setEnabled(has_current)
        self._rotate_right_btn.setEnabled(has_current)
        self._save_btn.setEnabled(has_current and self._rotation_angle % 360 != 0)
        self._copy_btn.setEnabled(has_current)
        self._delete_btn.setEnabled(has_current)

        if self._results:
            self._index_label.setText(
                f"Photo {self._current_index + 1} of {len(self._results)}"
            )
        else:
            self._index_label.setText("No results")

    def _go_previous(self) -> None:
        if self._current_index > 0:
            self._current_index -= 1
            self._zoom_factor = 1.0
            self._display_current()
            self._update_nav_state()

    def _go_next(self) -> None:
        if self._current_index < len(self._results) - 1:
            self._current_index += 1
            self._zoom_factor = 1.0
            self._display_current()
            self._update_nav_state()

    # ── Zoom ───────────────────────────────────────────────────

    def _zoom_in(self) -> None:
        self._zoom_factor = min(self._zoom_factor * 1.25, 5.0)
        self._apply_zoom()

    def _zoom_out(self) -> None:
        self._zoom_factor = max(self._zoom_factor / 1.25, 0.2)
        self._apply_zoom()

    def _zoom_reset(self) -> None:
        self._zoom_factor = 1.0
        self._apply_zoom()

    # ── Rotate / Save ─────────────────────────────────────────

    def _rotate_left(self) -> None:
        self._rotation_angle = (self._rotation_angle - 90) % 360
        self._apply_zoom()
        self._update_nav_state()

    def _rotate_right(self) -> None:
        self._rotation_angle = (self._rotation_angle + 90) % 360
        self._apply_zoom()
        self._update_nav_state()

    def _save_current(self) -> None:
        if self._current_index < 0 or self._current_index >= len(self._results):
            return
        if self._rotation_angle % 360 == 0:
            return
        assert self._library_root is not None and self._db_path is not None

        photo = self._results[self._current_index]
        image_path = self._library_root / photo.relative_path
        if not image_path.exists():
            QMessageBox.warning(self, "File missing", "The file no longer exists.")
            return

        try:
            img = Image.open(str(image_path))
            exif_data = img.info.get("exif")

            angle = self._rotation_angle % 360
            transpose_map = {
                90: Image.Transpose.ROTATE_270,
                180: Image.Transpose.ROTATE_180,
                270: Image.Transpose.ROTATE_90,
            }
            if angle in transpose_map:
                img = img.transpose(transpose_map[angle])

            save_kwargs: dict[str, object] = {}
            if exif_data:
                save_kwargs["exif"] = exif_data
            if image_path.suffix.lower() in (".jpg", ".jpeg", ".webp"):
                save_kwargs["quality"] = 95

            suffix = image_path.suffix
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp_path = Path(tmp.name)
            try:
                img.save(str(tmp_path), **save_kwargs)
                new_md5 = self._compute_md5(tmp_path)
                old_md5 = photo.md5

                if new_md5 == old_md5:
                    self._rotation_angle = 0
                    self._apply_zoom()
                    self._update_nav_state()
                    return
                if self._repository.exists_md5(self._db_path, new_md5):
                    QMessageBox.warning(
                        self,
                        "Duplicate",
                        "This rotated image is identical to an existing photo in the library. "
                        "Save cancelled to avoid duplicate.",
                    )
                    return

                shutil.move(str(tmp_path), str(image_path))
            finally:
                if tmp_path.exists():
                    tmp_path.unlink(missing_ok=True)

            self._repository.delete_by_md5(self._db_path, old_md5)
            self._repository.insert_photo(
                db_path=self._db_path,
                md5=new_md5,
                relative_path=photo.relative_path,
                capture_time_iso=photo.capture_time,
                tags_json=photo.tags,
                autotags_json=photo.autotags,
            )
            photo.md5 = new_md5

            self._rotation_angle = 0
            self._current_pixmap = QPixmap(str(image_path))
            self._apply_zoom()
            self._update_nav_state()
            self._populate_results_table()
            self._index_label.setText(
                f"Photo {self._current_index + 1} of {len(self._results)} — Saved!"
            )
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Save failed", str(exc))

    @staticmethod
    def _compute_md5(path: Path) -> str:
        digest = hashlib.md5()
        with path.open("rb") as fh:
            while True:
                chunk = fh.read(8192)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()

    # ── Copy / Delete ──────────────────────────────────────────

    def _copy_current(self) -> None:
        if self._current_index < 0 or self._current_index >= len(self._results):
            return
        assert self._library_root is not None
        photo = self._results[self._current_index]
        image_path = self._library_root / photo.relative_path
        if not image_path.exists():
            QMessageBox.warning(self, "File missing", "The file no longer exists.")
            return

        clipboard = QGuiApplication.clipboard()
        if clipboard is None:
            return
        mime_data = QMimeData()
        mime_data.setUrls([QUrl.fromLocalFile(str(image_path))])
        pixmap = QPixmap(str(image_path))
        if not pixmap.isNull():
            mime_data.setImageData(pixmap.toImage())
        clipboard.setMimeData(mime_data)
        self._index_label.setText(
            f"Photo {self._current_index + 1} of {len(self._results)} — Copied!"
        )

    def _delete_current(self) -> None:
        if self._current_index < 0 or self._current_index >= len(self._results):
            return
        assert self._library_root is not None and self._db_path is not None
        photo = self._results[self._current_index]
        image_path = self._library_root / photo.relative_path

        confirm = QMessageBox.question(
            self,
            "Delete photo",
            f"Delete this photo permanently?\n{photo.relative_path}",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        try:
            if image_path.exists():
                image_path.unlink()
            self._repository.delete_by_md5(self._db_path, photo.md5)

            self._results.pop(self._current_index)
            if self._current_index >= len(self._results):
                self._current_index = len(self._results) - 1

            self._populate_results_table()
            self._display_current()
            self._update_nav_state()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Delete failed", str(exc))
