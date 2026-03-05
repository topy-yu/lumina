from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.services.config_service import ConfigService
from app.services.photo_import_service import FileImportResult, ImportSummary, PhotoImportService


class ImportPage(QWidget):
    def __init__(
        self,
        config_service: ConfigService,
        import_service: PhotoImportService,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._config_service = config_service
        self._import_service = import_service
        self._latest_summary: ImportSummary | None = None
        self._latest_library_root: Path | None = None
        self._folder_tag_rules: dict[str, list[str]] = {}

        self._source_list = QListWidget()
        self._folder_combo = QComboBox()
        self._folder_combo.setMinimumWidth(260)
        self._tags_input = QLineEdit()
        self._tags_input.setPlaceholderText("tag1, tag2, ...")
        self._rules_list = QListWidget()
        self._rules_list.setMaximumHeight(96)
        self._summary = QTextEdit()
        self._summary.setReadOnly(True)
        self._details_table = QTableWidget(0, 6)
        self._details_table.setHorizontalHeaderLabels(
            ["Status", "Source", "Stored Path", "Tags", "Reason", "Actions"]
        )
        self._details_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._details_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._details_table.verticalHeader().setVisible(False)
        self._details_table.horizontalHeader().setStretchLastSection(True)
        self._details_table.setColumnWidth(0, 130)
        self._details_table.setColumnWidth(1, 280)
        self._details_table.setColumnWidth(2, 220)
        self._details_table.setColumnWidth(3, 180)
        self._details_table.setColumnWidth(4, 220)
        self._details_table.setColumnWidth(5, 280)
        self._status = QLabel("Add files or folders to begin.")

        self._import_button = QPushButton("Import")
        self._import_button.clicked.connect(self._run_import)  # type: ignore[arg-type]
        self._delete_duplicates_button = QPushButton("Delete all duplicates")
        self._delete_duplicates_button.setEnabled(False)
        self._delete_duplicates_button.clicked.connect(self._delete_all_duplicates)  # type: ignore[arg-type]

        self._build_ui()
        self.refresh_enabled_state()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        controls = QHBoxLayout()
        add_files_btn = QPushButton("Add files")
        add_files_btn.clicked.connect(self._add_files)  # type: ignore[arg-type]
        add_dir_btn = QPushButton("Add folder")
        add_dir_btn.clicked.connect(self._add_folder)  # type: ignore[arg-type]
        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self._source_list.clear)  # type: ignore[arg-type]
        apply_rule_btn = QPushButton("Apply tags to folder")
        apply_rule_btn.clicked.connect(self._apply_folder_tags_rule)  # type: ignore[arg-type]
        remove_rule_btn = QPushButton("Remove selected rule")
        remove_rule_btn.clicked.connect(self._remove_selected_rule)  # type: ignore[arg-type]
        clear_rules_btn = QPushButton("Clear rules")
        clear_rules_btn.clicked.connect(self._clear_rules)  # type: ignore[arg-type]

        controls.addWidget(add_files_btn)
        controls.addWidget(add_dir_btn)
        controls.addWidget(clear_btn)
        controls.addWidget(self._import_button)
        controls.addWidget(self._delete_duplicates_button)

        tags_controls = QHBoxLayout()
        tags_controls.addWidget(QLabel("Folder:"))
        tags_controls.addWidget(self._folder_combo)
        tags_controls.addWidget(QLabel("Tags:"))
        tags_controls.addWidget(self._tags_input)
        tags_controls.addWidget(apply_rule_btn)
        tags_controls.addWidget(remove_rule_btn)
        tags_controls.addWidget(clear_rules_btn)

        layout.addLayout(controls)
        layout.addWidget(self._source_list)
        layout.addLayout(tags_controls)
        layout.addWidget(self._rules_list)
        layout.addWidget(self._status)
        layout.addWidget(self._summary)
        layout.addWidget(self._details_table)

    def refresh_enabled_state(self) -> None:
        config = self._config_service.load()
        errors = self._config_service.validate(config)
        enabled = len(errors) == 0
        self._import_button.setEnabled(enabled)
        if not enabled:
            self._status.setText("Configure a valid photo library folder on page 1.")
        else:
            self._status.setText("Ready to import.")

    def _add_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Select photos",
            filter="Images (*.jpg *.jpeg *.png *.webp *.heic *.tif *.tiff *.bmp);;All Files (*)",
        )
        for file in files:
            path = Path(file)
            self._source_list.addItem(str(path))
            self._add_folder_option(path.parent)

    def _add_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Select folder to scan for photos")
        if not folder:
            return
        folder_path = Path(folder)
        self._add_folder_option(folder_path)
        for path in self._import_service.collect_supported_files(Path(folder)):
            self._source_list.addItem(str(path))

    def _run_import(self) -> None:
        file_paths = [Path(self._source_list.item(i).text()) for i in range(self._source_list.count())]
        if not file_paths:
            QMessageBox.information(self, "No files", "Please add files or a folder first.")
            return

        config = self._config_service.load()
        errors = self._config_service.validate(config)
        if errors:
            QMessageBox.warning(self, "Invalid settings", "\n".join(errors))
            self.refresh_enabled_state()
            return

        folder_tags_map = self._collect_valid_folder_rules()
        summary = self._import_service.import_files(
            file_paths,
            config,
            folder_tags_map=folder_tags_map,
        )
        self._latest_summary = summary
        self._latest_library_root = Path(config.library_root)
        self._summary.setPlainText(self._format_summary(summary))
        self._populate_details(summary, Path(config.library_root))
        self._refresh_duplicate_delete_state()
        self._status.setText("Import finished.")

    def _format_summary(self, summary: ImportSummary) -> str:
        lines = [
            f"Processed: {summary.total}",
            f"Imported: {summary.imported}",
            f"Duplicates: {summary.duplicates}",
            f"Skipped (no time): {summary.skipped_no_capture_time}",
            f"Errors: {summary.errors}",
            "",
            "Details:",
        ]
        lines.extend(self._format_result(result) for result in summary.results)
        return "\n".join(lines)

    @staticmethod
    def _format_result(result: FileImportResult) -> str:
        rel = result.relative_path if result.relative_path else "-"
        tags = ", ".join(result.applied_tags) if result.applied_tags else "-"
        reason = result.reason if result.reason else "-"
        return f"[{result.status}] {result.source} -> {rel} | tags={tags} | {reason}"

    def _populate_details(self, summary: ImportSummary, library_root: Path) -> None:
        self._details_table.setRowCount(len(summary.results))
        for row, result in enumerate(summary.results):
            status_item = QTableWidgetItem(result.status)
            source_item = QTableWidgetItem(result.source)
            stored_item = QTableWidgetItem(result.relative_path or "-")
            tags_item = QTableWidgetItem(", ".join(result.applied_tags) if result.applied_tags else "-")
            reason_item = QTableWidgetItem(result.reason or "-")

            self._details_table.setItem(row, 0, status_item)
            self._details_table.setItem(row, 1, source_item)
            self._details_table.setItem(row, 2, stored_item)
            self._details_table.setItem(row, 3, tags_item)
            self._details_table.setItem(row, 4, reason_item)

            actions = QWidget()
            actions_layout = QHBoxLayout(actions)
            actions_layout.setContentsMargins(0, 0, 0, 0)

            preview_btn = QPushButton("Preview")
            preview_btn.clicked.connect(  # type: ignore[arg-type]
                lambda _checked=False, r=row: self._preview_row(r)
            )
            rename_btn = QPushButton("Rename")
            rename_btn.clicked.connect(  # type: ignore[arg-type]
                lambda _checked=False, r=row: self._rename_row(r)
            )
            delete_btn = QPushButton("Delete")
            delete_btn.clicked.connect(  # type: ignore[arg-type]
                lambda _checked=False, r=row: self._delete_row(r)
            )

            action_target = self._resolve_action_target(result, library_root)
            if action_target is None:
                preview_btn.setEnabled(False)
                rename_btn.setEnabled(False)
                delete_btn.setEnabled(False)

            actions_layout.addWidget(preview_btn)
            actions_layout.addWidget(rename_btn)
            actions_layout.addWidget(delete_btn)
            self._details_table.setCellWidget(row, 5, actions)

    def _add_folder_option(self, folder: Path) -> None:
        resolved = str(folder.resolve(strict=False))
        if self._folder_combo.findText(resolved) == -1:
            self._folder_combo.addItem(resolved)

    @staticmethod
    def _parse_tags(text: str) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for tag in text.split(","):
            clean = tag.strip()
            if not clean or clean in seen:
                continue
            seen.add(clean)
            normalized.append(clean)
        return normalized

    def _apply_folder_tags_rule(self) -> None:
        folder = self._folder_combo.currentText().strip()
        if not folder:
            QMessageBox.information(self, "Folder required", "Please add/select a folder first.")
            return
        tags = self._parse_tags(self._tags_input.text())
        if not tags:
            QMessageBox.information(self, "Tags required", "Please input at least one tag.")
            return
        existing = self._folder_tag_rules.get(folder, [])
        merged = existing + [t for t in tags if t not in existing]
        self._folder_tag_rules[folder] = merged
        self._rules_list.clear()
        for key, value in self._folder_tag_rules.items():
            self._rules_list.addItem(f"{key} -> {', '.join(value)}")
        self._tags_input.clear()

    def _remove_selected_rule(self) -> None:
        item = self._rules_list.currentItem()
        if item is None:
            return
        row = self._rules_list.row(item)
        self._rules_list.takeItem(row)
        keys = list(self._folder_tag_rules.keys())
        if row < len(keys):
            self._folder_tag_rules.pop(keys[row], None)

    def _clear_rules(self) -> None:
        self._folder_tag_rules.clear()
        self._rules_list.clear()

    def _collect_valid_folder_rules(self) -> dict[str, list[str]]:
        valid: dict[str, list[str]] = {}
        skipped: list[str] = []
        for folder, tags in self._folder_tag_rules.items():
            folder_path = Path(folder)
            if not folder_path.exists() or not folder_path.is_dir():
                skipped.append(folder)
                continue
            valid[str(folder_path.resolve(strict=False))] = tags
        if skipped:
            QMessageBox.warning(
                self,
                "Skipped invalid rules",
                "These folders are unavailable and were skipped:\n" + "\n".join(skipped),
            )
        return valid

    @staticmethod
    def _resolve_preview_path(result: FileImportResult, library_root: Path) -> Path | None:
        if result.relative_path:
            stored_path = library_root / result.relative_path
            if stored_path.exists() and stored_path.is_file():
                return stored_path

        source_path = Path(result.source)
        if source_path.exists() and source_path.is_file():
            return source_path
        return None

    @staticmethod
    def _resolve_action_target(result: FileImportResult, library_root: Path) -> tuple[Path, str] | None:
        if result.relative_path:
            stored_path = library_root / result.relative_path
            if stored_path.exists() and stored_path.is_file():
                return stored_path, "library"

        source_path = Path(result.source)
        if source_path.exists() and source_path.is_file():
            return source_path, "source"
        return None

    def _preview_row(self, row: int) -> None:
        if self._latest_summary is None or self._latest_library_root is None:
            return
        result = self._latest_summary.results[row]
        preview_path = self._resolve_preview_path(result, self._latest_library_root)
        if preview_path is None:
            QMessageBox.information(self, "File missing", "File is no longer available.")
            return
        self._show_preview(preview_path)

    def _rename_row(self, row: int) -> None:
        if self._latest_summary is None or self._latest_library_root is None:
            return
        result = self._latest_summary.results[row]
        action_target = self._resolve_action_target(result, self._latest_library_root)
        if action_target is None:
            QMessageBox.information(self, "File missing", "File is no longer available.")
            return
        old_path, target_kind = action_target

        entered, ok = QInputDialog.getText(self, "Rename file", "New file name:", text=old_path.name)
        if not ok:
            return
        new_name = entered.strip()
        if not new_name:
            QMessageBox.warning(self, "Invalid name", "File name cannot be empty.")
            return
        if Path(new_name).name != new_name:
            QMessageBox.warning(self, "Invalid name", "Please input a file name only, not a path.")
            return
        if Path(new_name).suffix == "":
            new_name = f"{new_name}{old_path.suffix}"

        new_path = old_path.with_name(new_name)
        if new_path.exists():
            QMessageBox.warning(self, "Name exists", f"File already exists:\n{new_path}")
            return

        try:
            old_path.rename(new_path)
            if target_kind == "library" and result.relative_path:
                config = self._config_service.load()
                old_relative = result.relative_path
                new_relative = str(new_path.relative_to(self._latest_library_root))
                self._import_service.update_relative_path_record(config, old_relative, new_relative)
                result.relative_path = new_relative
            else:
                result.source = str(new_path)
            result.reason = "renamed by user"
            self._populate_details(self._latest_summary, self._latest_library_root)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Rename failed", str(exc))

    def _delete_row(self, row: int) -> None:
        if self._latest_summary is None or self._latest_library_root is None:
            return
        result = self._latest_summary.results[row]
        action_target = self._resolve_action_target(result, self._latest_library_root)
        if action_target is None:
            QMessageBox.information(self, "File missing", "File is no longer available.")
            return
        target_path, target_kind = action_target
        confirm = QMessageBox.question(
            self,
            "Delete file",
            f"Delete this file?\n{target_path}",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        try:
            target_path.unlink()
            if target_kind == "library" and result.relative_path:
                config = self._config_service.load()
                self._import_service.delete_relative_path_record(config, result.relative_path)
                result.relative_path = None
            result.status = "deleted"
            result.reason = "deleted by user"
            self._populate_details(self._latest_summary, self._latest_library_root)
            self._refresh_duplicate_delete_state()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Delete failed", str(exc))

    def _delete_all_duplicates(self) -> None:
        if self._latest_summary is None or self._latest_library_root is None:
            QMessageBox.information(self, "No results", "Run an import first.")
            return

        duplicate_rows = [
            idx
            for idx, result in enumerate(self._latest_summary.results)
            if result.status == "duplicate"
        ]
        if not duplicate_rows:
            QMessageBox.information(self, "No duplicates", "No duplicate files to delete.")
            self._refresh_duplicate_delete_state()
            return

        confirm = QMessageBox.question(
            self,
            "Delete all duplicates",
            f"Delete {len(duplicate_rows)} duplicate source files?",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        deleted = 0
        failed = 0
        for idx in duplicate_rows:
            result = self._latest_summary.results[idx]
            source_path = Path(result.source)
            if not source_path.exists() or not source_path.is_file():
                failed += 1
                result.reason = "source file not found"
                continue
            try:
                source_path.unlink()
                deleted += 1
                result.status = "duplicate-deleted"
                result.reason = "deleted by user"
            except Exception as exc:  # noqa: BLE001
                failed += 1
                result.reason = f"delete failed: {exc}"

        self._populate_details(self._latest_summary, self._latest_library_root)
        self._refresh_duplicate_delete_state()
        self._status.setText(f"Deleted {deleted} duplicate file(s); failed {failed}.")

    def _refresh_duplicate_delete_state(self) -> None:
        if self._latest_summary is None:
            self._delete_duplicates_button.setEnabled(False)
            return
        has_duplicates = any(result.status == "duplicate" for result in self._latest_summary.results)
        self._delete_duplicates_button.setEnabled(has_duplicates)

    def _show_preview(self, image_path: Path) -> None:
        pixmap = QPixmap(str(image_path))
        if pixmap.isNull():
            QMessageBox.warning(
                self,
                "Preview not available",
                f"Cannot preview this file format or file is invalid:\n{image_path}",
            )
            return

        dialog = QDialog(self)
        dialog.setWindowTitle(f"Preview - {image_path.name}")
        dialog.resize(900, 700)

        layout = QVBoxLayout(dialog)
        image_label = QLabel()
        image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        image_label.setPixmap(
            pixmap.scaled(
                860,
                620,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        layout.addWidget(image_label)
        dialog.exec()

