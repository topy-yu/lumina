from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.services.ai_model_service import get_provider
from app.services.config_service import AppConfig, ConfigService
from app.services.photo_import_service import FileImportResult, ImportSummary, PhotoImportService
from app.services.preimport_service import PreImportItemReport, PreImportJobState, PreImportService


class _ImportWorker(QThread):
    progress = Signal(str)
    import_done = Signal(object)

    def __init__(
        self,
        import_service: PhotoImportService,
        files: list[Path],
        config: AppConfig,
        folder_tags_map: dict[str, list[str]],
        folder_capture_time_map: dict[str, str],
        require_model_ready: bool,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._import_service = import_service
        self._files = files
        self._config = config
        self._folder_tags_map = folder_tags_map
        self._folder_capture_time_map = folder_capture_time_map
        self._require_model_ready = require_model_ready

    def run(self) -> None:
        summary = self._import_service.import_files(
            self._files,
            self._config,
            folder_tags_map=self._folder_tags_map,
            folder_capture_time_map=self._folder_capture_time_map,
            progress=self.progress.emit,
            require_model_ready=self._require_model_ready,
        )
        self.import_done.emit(summary)


class _PreImportWorker(QThread):
    progress = Signal(str)
    preimport_done = Signal(object)

    def __init__(
        self,
        preimport_service: PreImportService,
        config: AppConfig,
        *,
        mode: str,
        files: list[Path] | None = None,
        folder_tags_map: dict[str, list[str]] | None = None,
        folder_capture_time_map: dict[str, str] | None = None,
        job_id: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._service = preimport_service
        self._config = config
        self._mode = mode
        self._files = files or []
        self._folder_tags_map = folder_tags_map or {}
        self._folder_capture_time_map = folder_capture_time_map or {}
        self._job_id = job_id
        self._stop_requested = False

    def request_stop(self) -> None:
        self._stop_requested = True

    def run(self) -> None:
        if self._mode == "prepare":
            job_id = self._service.create_job(
                self._files,
                self._config,
                self._folder_tags_map,
                self._folder_capture_time_map,
            )
            state = self._service.run_preimport(
                job_id,
                self._config,
                progress=self.progress.emit,
                should_stop=lambda: self._stop_requested,
            )
            self.preimport_done.emit(("prepare", state))
            return

        if self._job_id is None:
            raise RuntimeError("job_id is required for resume/import")

        if self._mode == "resume":
            state = self._service.run_preimport(
                self._job_id,
                self._config,
                progress=self.progress.emit,
                should_stop=lambda: self._stop_requested,
            )
            self.preimport_done.emit(("resume", state))
            return

        if self._mode == "import":
            summary = self._service.import_prepared(
                self._job_id,
                self._config,
                progress=self.progress.emit,
            )
            self.preimport_done.emit(("import", summary))


class _RetryItemWorker(QThread):
    retry_done = Signal(object)

    def __init__(
        self,
        preimport_service: PreImportService,
        job_id: str,
        item_id: int,
        config: AppConfig,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._service = preimport_service
        self._job_id = job_id
        self._item_id = item_id
        self._config = config

    def run(self) -> None:
        self._service.prepare_single_item(self._job_id, self._item_id, self._config)
        state = self._service.get_job_state(self._job_id)
        self.retry_done.emit(state)


class ImportPage(QWidget):
    def __init__(
        self,
        config_service: ConfigService,
        import_service: PhotoImportService,
        preimport_service: PreImportService,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._config_service = config_service
        self._import_service = import_service
        self._preimport_service = preimport_service
        self._latest_summary: ImportSummary | None = None
        self._latest_library_root: Path | None = None
        self._import_worker: _ImportWorker | None = None
        self._preimport_worker: _PreImportWorker | None = None
        self._active_preimport_job_id: str | None = self._preimport_service.latest_active_job_id()
        self._folder_tag_rules: dict[str, list[str]] = {}
        self._folder_capture_time_rules: dict[str, str] = {}
        self._latest_preimport_items: list[PreImportItemReport] | None = None
        self._preimport_details_active: bool = False
        self._suppress_cell_changed: bool = False
        self._retry_item_worker: _RetryItemWorker | None = None

        self._source_list = QListWidget()
        self._folder_combo = QComboBox()
        self._folder_combo.setMinimumWidth(260)
        self._tags_input = QLineEdit()
        self._tags_input.setPlaceholderText("tag1, tag2, ...")
        self._capture_time_input = QLineEdit()
        self._capture_time_input.setPlaceholderText("YYYY-MM-DD HH:MM:SS")
        self._rules_list = QListWidget()
        self._rules_list.setMaximumHeight(96)
        self._summary = QTextEdit()
        self._summary.setReadOnly(True)
        self._details_table = QTableWidget(0, 7)
        self._details_table.setHorizontalHeaderLabels(
            ["Status", "Source", "Stored Path", "Tags", "Auto Tags", "Reason", "Actions"]
        )
        self._details_table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.SelectedClicked
        )
        self._details_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._details_table.verticalHeader().setVisible(False)
        self._details_table.horizontalHeader().setStretchLastSection(True)
        self._details_table.setColumnWidth(0, 130)
        self._details_table.setColumnWidth(1, 250)
        self._details_table.setColumnWidth(2, 200)
        self._details_table.setColumnWidth(3, 150)
        self._details_table.setColumnWidth(4, 180)
        self._details_table.setColumnWidth(5, 200)
        self._details_table.setColumnWidth(6, 280)
        self._status = QLabel("Add files or folders to begin.")

        self._import_button = QPushButton("Direct Import")
        self._import_button.clicked.connect(self._run_import)  # type: ignore[arg-type]
        self._preimport_button = QPushButton("Pre-Import")
        self._preimport_button.clicked.connect(self._run_preimport)  # type: ignore[arg-type]
        self._resume_preimport_button = QPushButton("Resume Pre-Import")
        self._resume_preimport_button.clicked.connect(self._resume_preimport)  # type: ignore[arg-type]
        self._stop_preimport_button = QPushButton("Stop Pre-Import")
        self._stop_preimport_button.clicked.connect(self._stop_preimport)  # type: ignore[arg-type]
        self._import_prepared_button = QPushButton("Import Prepared")
        self._import_prepared_button.clicked.connect(self._run_import_prepared)  # type: ignore[arg-type]
        self._allow_import_without_model = QCheckBox("Allow import without autotag if model unavailable")
        self._allow_import_without_model.setChecked(False)
        self._allow_import_without_model.toggled.connect(self.refresh_enabled_state)  # type: ignore[arg-type]
        self._delete_duplicates_button = QPushButton("Delete all duplicates")
        self._delete_duplicates_button.setEnabled(False)
        self._delete_duplicates_button.clicked.connect(self._delete_all_duplicates)  # type: ignore[arg-type]
        self._retry_all_failed_button = QPushButton("Retry All Failed")
        self._retry_all_failed_button.setEnabled(False)
        self._retry_all_failed_button.clicked.connect(self._retry_all_failed)  # type: ignore[arg-type]
        self._preimport_status = QLabel()

        self._details_table.cellChanged.connect(self._on_details_cell_changed)  # type: ignore[arg-type]

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
        apply_rule_btn = QPushButton("Apply folder rule")
        apply_rule_btn.clicked.connect(self._apply_folder_rule)  # type: ignore[arg-type]
        remove_rule_btn = QPushButton("Remove selected rule")
        remove_rule_btn.clicked.connect(self._remove_selected_rule)  # type: ignore[arg-type]
        clear_rules_btn = QPushButton("Clear rules")
        clear_rules_btn.clicked.connect(self._clear_rules)  # type: ignore[arg-type]

        controls.addWidget(add_files_btn)
        controls.addWidget(add_dir_btn)
        controls.addWidget(clear_btn)
        controls.addWidget(self._preimport_button)
        controls.addWidget(self._resume_preimport_button)
        controls.addWidget(self._stop_preimport_button)
        controls.addWidget(self._import_prepared_button)
        controls.addWidget(self._import_button)
        controls.addWidget(self._allow_import_without_model)
        controls.addWidget(self._delete_duplicates_button)
        controls.addWidget(self._retry_all_failed_button)

        tags_controls = QHBoxLayout()
        tags_controls.addWidget(QLabel("Folder:"))
        tags_controls.addWidget(self._folder_combo)
        tags_controls.addWidget(QLabel("Tags:"))
        tags_controls.addWidget(self._tags_input)
        tags_controls.addWidget(QLabel("Capture time fallback:"))
        tags_controls.addWidget(self._capture_time_input)
        tags_controls.addWidget(apply_rule_btn)
        tags_controls.addWidget(remove_rule_btn)
        tags_controls.addWidget(clear_rules_btn)

        layout.addLayout(controls)
        layout.addWidget(self._source_list)
        layout.addLayout(tags_controls)
        layout.addWidget(self._rules_list)
        layout.addWidget(self._status)
        layout.addWidget(self._preimport_status)
        layout.addWidget(self._summary)
        layout.addWidget(self._details_table)

    def refresh_enabled_state(self) -> None:
        config = self._config_service.load()
        errors = self._config_service.validate(config)
        enabled = len(errors) == 0
        model_error = ""
        require_model_ready = not self._allow_import_without_model.isChecked()
        if enabled and config.ai_api_url and config.ai_model_name:
            try:
                provider = get_provider(config.ai_provider)
                model_status = provider.check_model(config.ai_api_url, config.ai_model_name)
                model_ready = model_status.connected and model_status.loaded
                if require_model_ready:
                    enabled = model_ready
                if not model_ready:
                    model_error = model_status.message or "AI model is not ready."
            except Exception as exc:  # noqa: BLE001
                if require_model_ready:
                    enabled = False
                model_error = f"AI model check failed: {exc}"
        self._import_button.setEnabled(enabled)
        if not enabled:
            if errors:
                self._status.setText("Configure a valid photo library folder on page 1.")
            elif model_error:
                if require_model_ready:
                    self._status.setText(f"Model not ready: {model_error}")
                else:
                    self._status.setText(f"Model unavailable, autotag will be skipped: {model_error}")
            else:
                self._status.setText("Import is currently unavailable.")
        else:
            self._status.setText("Ready to import.")
        self._preimport_button.setEnabled(self._preimport_worker is None and len(errors) == 0)
        self._stop_preimport_button.setEnabled(self._preimport_worker is not None)
        can_resume = self._active_preimport_job_id is not None and self._preimport_worker is None
        self._resume_preimport_button.setEnabled(can_resume)
        self._import_prepared_button.setEnabled(can_resume and len(errors) == 0)
        self._refresh_preimport_status()

    def _add_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Select photos",
            filter="Images (*.jpg *.jpeg *.png *.webp *.heic *.tif *.tiff *.bmp);;All Files (*)",
        )
        paths = [Path(file) for file in files]
        self._add_source_paths(paths)

    def _add_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Select folder to scan for photos")
        if not folder:
            return
        folder_path = Path(folder)
        self._add_folder_tree_options(folder_path)
        paths = self._import_service.collect_supported_files(folder_path)
        self._add_source_paths(paths)

    def _run_import(self) -> None:
        if self._import_worker is not None:
            return
        file_paths = self._deduplicate_paths(
            [Path(self._source_list.item(i).text()) for i in range(self._source_list.count())]
        )
        if not file_paths:
            QMessageBox.information(self, "No files", "Please add files or a folder first.")
            return

        config = self._config_service.load()
        errors = self._config_service.validate(config)
        if errors:
            QMessageBox.warning(self, "Invalid settings", "\n".join(errors))
            self.refresh_enabled_state()
            return
        if config.ai_api_url and config.ai_model_name:
            require_model_ready = not self._allow_import_without_model.isChecked()
            try:
                provider = get_provider(config.ai_provider)
                model_status = provider.check_model(config.ai_api_url, config.ai_model_name)
                model_ready = model_status.connected and model_status.loaded
                if require_model_ready and not model_ready:
                    QMessageBox.warning(
                        self,
                        "Model not ready",
                        f"AI model is not ready for import:\n{model_status.message}",
                    )
                    self.refresh_enabled_state()
                    return
            except Exception as exc:  # noqa: BLE001
                if require_model_ready:
                    QMessageBox.warning(
                        self,
                        "Model check failed",
                        f"Cannot verify AI model before import:\n{exc}",
                    )
                    self.refresh_enabled_state()
                    return

        folder_tags_map, folder_capture_time_map = self._collect_valid_folder_rules()

        self._import_button.setEnabled(False)
        self._status.setText("Importing...")
        self._latest_library_root = Path(config.library_root)

        self._import_worker = _ImportWorker(
            self._import_service, file_paths, config,
            folder_tags_map,
            folder_capture_time_map,
            require_model_ready=not self._allow_import_without_model.isChecked(),
            parent=self,
        )
        self._import_worker.progress.connect(self._on_import_progress)  # type: ignore[arg-type]
        self._import_worker.import_done.connect(self._on_import_done)  # type: ignore[arg-type]
        self._import_worker.start()

    def _run_preimport(self) -> None:
        if self._preimport_worker is not None:
            return
        file_paths = self._deduplicate_paths(
            [Path(self._source_list.item(i).text()) for i in range(self._source_list.count())]
        )
        if not file_paths:
            QMessageBox.information(self, "No files", "Please add files or a folder first.")
            return
        config = self._config_service.load()
        errors = self._config_service.validate(config)
        if errors:
            QMessageBox.warning(self, "Invalid settings", "\n".join(errors))
            self.refresh_enabled_state()
            return
        folder_tags_map, folder_capture_time_map = self._collect_valid_folder_rules()
        self._preimport_worker = _PreImportWorker(
            self._preimport_service,
            config,
            mode="prepare",
            files=file_paths,
            folder_tags_map=folder_tags_map,
            folder_capture_time_map=folder_capture_time_map,
            parent=self,
        )
        self._preimport_worker.progress.connect(self._on_import_progress)  # type: ignore[arg-type]
        self._preimport_worker.preimport_done.connect(self._on_preimport_done)  # type: ignore[arg-type]
        self._status.setText("Pre-import running...")
        self.refresh_enabled_state()
        self._preimport_worker.start()

    def _resume_preimport(self) -> None:
        if self._preimport_worker is not None or self._active_preimport_job_id is None:
            return
        config = self._config_service.load()
        errors = self._config_service.validate(config)
        if errors:
            QMessageBox.warning(self, "Invalid settings", "\n".join(errors))
            self.refresh_enabled_state()
            return
        self._preimport_worker = _PreImportWorker(
            self._preimport_service,
            config,
            mode="resume",
            job_id=self._active_preimport_job_id,
            parent=self,
        )
        self._preimport_worker.progress.connect(self._on_import_progress)  # type: ignore[arg-type]
        self._preimport_worker.preimport_done.connect(self._on_preimport_done)  # type: ignore[arg-type]
        self._status.setText("Resuming pre-import...")
        self.refresh_enabled_state()
        self._preimport_worker.start()

    def _stop_preimport(self) -> None:
        if self._preimport_worker is None:
            return
        self._preimport_worker.request_stop()
        self._status.setText("Stopping pre-import...")

    def _run_import_prepared(self) -> None:
        if self._preimport_worker is not None:
            return
        if self._active_preimport_job_id is None:
            QMessageBox.information(self, "No pre-import job", "Run pre-import first.")
            return
        config = self._config_service.load()
        errors = self._config_service.validate(config)
        if errors:
            QMessageBox.warning(self, "Invalid settings", "\n".join(errors))
            self.refresh_enabled_state()
            return
        self._preimport_worker = _PreImportWorker(
            self._preimport_service,
            config,
            mode="import",
            job_id=self._active_preimport_job_id,
            parent=self,
        )
        self._preimport_worker.progress.connect(self._on_import_progress)  # type: ignore[arg-type]
        self._preimport_worker.preimport_done.connect(self._on_preimport_done)  # type: ignore[arg-type]
        self._status.setText("Importing prepared items...")
        self.refresh_enabled_state()
        self._preimport_worker.start()

    def _on_import_progress(self, message: str) -> None:
        self._status.setText(message)

    def _on_import_done(self, result: object) -> None:
        assert isinstance(result, ImportSummary)
        self._import_worker = None

        self._latest_summary = result
        assert self._latest_library_root is not None
        self._summary.setPlainText(self._format_summary(result))
        self._populate_details(result, self._latest_library_root)
        self._refresh_duplicate_delete_state()
        if result.aborted:
            self._status.setText(result.abort_reason or "Import aborted.")
        else:
            self._status.setText("Import finished.")
        self.refresh_enabled_state()

    def _on_preimport_done(self, payload: object) -> None:
        self._preimport_worker = None
        if not isinstance(payload, tuple) or len(payload) != 2:
            self._status.setText("Pre-import finished with unexpected result.")
            self.refresh_enabled_state()
            return

        mode, result = payload
        if mode in ("prepare", "resume") and isinstance(result, PreImportJobState):
            self._active_preimport_job_id = result.job_id
            self._status.setText(f"Pre-import complete: {result.prepared} prepared, {result.failed} failed.")
            report_items = self._preimport_service.list_job_items(result.job_id)
            self._latest_preimport_items = report_items
            self._summary.setPlainText(self._format_preimport_summary(result, report_items))
            self._populate_preimport_details(report_items)
        elif mode == "import" and isinstance(result, ImportSummary):
            self._latest_summary = result
            self._latest_preimport_items = None
            self._preimport_details_active = False
            config = self._config_service.load()
            self._latest_library_root = Path(config.library_root)
            self._summary.setPlainText(self._format_summary(result))
            self._populate_details(result, self._latest_library_root)
            if result.aborted:
                self._status.setText(result.abort_reason or "Import prepared aborted.")
            else:
                self._status.setText("Import prepared finished.")
            if self._active_preimport_job_id is not None:
                state = self._preimport_service.get_job_state(self._active_preimport_job_id)
                if state.planned == 0 and state.prepared == 0 and state.failed == 0:
                    self._active_preimport_job_id = None
        else:
            self._status.setText("Pre-import finished.")
        self._refresh_duplicate_delete_state()
        self.refresh_enabled_state()

    @staticmethod
    def _format_preimport_summary(
        state: PreImportJobState,
        items: list[PreImportItemReport],
    ) -> str:
        lines = [
            f"Job ID: {state.job_id}",
            f"Status: {state.status}",
            f"Planned: {state.planned}",
            f"Prepared: {state.prepared}",
            f"Failed: {state.failed}",
            f"Imported: {state.imported}",
        ]
        return "\n".join(lines)

    def _populate_preimport_details(self, items: list[PreImportItemReport]) -> None:
        self._suppress_cell_changed = True
        self._preimport_details_active = True
        self._details_table.clearContents()
        self._details_table.setRowCount(0)
        self._details_table.setColumnCount(8)
        self._details_table.setHorizontalHeaderLabels(
            ["State", "Source", "Planned Path", "Capture Time", "Tags", "AutoTags", "Error", "Actions"]
        )
        self._details_table.setColumnWidth(0, 80)
        self._details_table.setColumnWidth(1, 220)
        self._details_table.setColumnWidth(2, 200)
        self._details_table.setColumnWidth(3, 160)
        self._details_table.setColumnWidth(4, 160)
        self._details_table.setColumnWidth(5, 160)
        self._details_table.setColumnWidth(6, 200)
        self._details_table.setColumnWidth(7, 160)

        self._details_table.setRowCount(len(items))
        for row, item in enumerate(items):
            state_item = QTableWidgetItem(item.state)
            state_item.setFlags(state_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            source_item = QTableWidgetItem(item.source_path)
            source_item.setFlags(source_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            source_item.setToolTip(item.source_path)
            planned_item = QTableWidgetItem(item.planned_relative_path or "-")
            planned_item.setFlags(planned_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            if item.planned_relative_path:
                planned_item.setToolTip(item.planned_relative_path)
            capture_item = QTableWidgetItem(item.capture_time_iso or "")
            capture_item.setFlags(capture_item.flags() | Qt.ItemFlag.ItemIsEditable)

            tags_list = json.loads(item.manual_tags_json) if item.manual_tags_json else []
            tags_item = QTableWidgetItem(", ".join(tags_list) if tags_list else "")
            tags_item.setFlags(tags_item.flags() | Qt.ItemFlag.ItemIsEditable)

            autotags_list = json.loads(item.autotags_json) if item.autotags_json else []
            autotags_item = QTableWidgetItem(", ".join(autotags_list) if autotags_list else "-")
            autotags_item.setFlags(autotags_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            if autotags_list:
                autotags_item.setToolTip(", ".join(autotags_list))

            error_item = QTableWidgetItem(item.error_message or "-")
            error_item.setFlags(error_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            if item.error_message:
                error_item.setToolTip(item.error_message)

            self._details_table.setItem(row, 0, state_item)
            self._details_table.setItem(row, 1, source_item)
            self._details_table.setItem(row, 2, planned_item)
            self._details_table.setItem(row, 3, capture_item)
            self._details_table.setItem(row, 4, tags_item)
            self._details_table.setItem(row, 5, autotags_item)
            self._details_table.setItem(row, 6, error_item)

            actions = QWidget()
            actions_layout = QHBoxLayout(actions)
            actions_layout.setContentsMargins(0, 0, 0, 0)

            preview_btn = QPushButton("Preview")
            preview_btn.clicked.connect(  # type: ignore[arg-type]
                lambda _checked=False, s=item.source_path: self._preview_source(s)
            )

            actions_layout.addWidget(preview_btn)

            if item.state == "failed":
                retry_btn = QPushButton("Retry")
                retry_btn.clicked.connect(  # type: ignore[arg-type]
                    lambda _checked=False, iid=item.item_id: self._retry_single_item(iid)
                )
                actions_layout.addWidget(retry_btn)

            self._details_table.setCellWidget(row, 7, actions)

        has_failed = any(it.state == "failed" for it in items)
        self._retry_all_failed_button.setEnabled(has_failed and self._preimport_worker is None)
        self._suppress_cell_changed = False

    def _on_details_cell_changed(self, row: int, column: int) -> None:
        if self._suppress_cell_changed or not self._preimport_details_active:
            return
        if self._latest_preimport_items is None or row >= len(self._latest_preimport_items):
            return

        item = self._latest_preimport_items[row]
        cell = self._details_table.item(row, column)
        if cell is None:
            return
        text = cell.text().strip()

        if column == 3:
            self._preimport_service.update_item_capture_time(item.item_id, text)
            item.capture_time_iso = text if text else None
        elif column == 4:
            tags = [t.strip() for t in text.split(",") if t.strip()]
            tags_json = json.dumps(tags, ensure_ascii=False)
            self._preimport_service.update_item_tags(item.item_id, tags_json)
            item.manual_tags_json = tags_json

    def _preview_source(self, source_path_str: str) -> None:
        source = Path(source_path_str)
        if not source.exists() or not source.is_file():
            QMessageBox.information(self, "File missing", "Source file is no longer available.")
            return
        self._show_preview(source)

    def _retry_single_item(self, item_id: int) -> None:
        if self._retry_item_worker is not None or self._preimport_worker is not None:
            return
        if self._active_preimport_job_id is None:
            return
        config = self._config_service.load()
        self._status.setText("Retrying item...")
        self._retry_item_worker = _RetryItemWorker(
            self._preimport_service,
            self._active_preimport_job_id,
            item_id,
            config,
            parent=self,
        )
        self._retry_item_worker.retry_done.connect(self._on_retry_item_done)  # type: ignore[arg-type]
        self._retry_item_worker.start()

    def _on_retry_item_done(self, state: object) -> None:
        self._retry_item_worker = None
        if not isinstance(state, PreImportJobState):
            self._status.setText("Retry finished.")
            self.refresh_enabled_state()
            return
        self._status.setText(f"Retry done: {state.prepared} prepared, {state.failed} failed.")
        report_items = self._preimport_service.list_job_items(state.job_id)
        self._latest_preimport_items = report_items
        self._summary.setPlainText(self._format_preimport_summary(state, report_items))
        self._populate_preimport_details(report_items)
        self.refresh_enabled_state()

    def _retry_all_failed(self) -> None:
        if self._active_preimport_job_id is None:
            QMessageBox.information(self, "No job", "No active pre-import job.")
            return
        if self._preimport_worker is not None or self._retry_item_worker is not None:
            return
        state = self._preimport_service.get_job_state(self._active_preimport_job_id)
        if state.failed == 0:
            QMessageBox.information(self, "No failed items", "There are no failed items to retry.")
            return
        config = self._config_service.load()
        errors = self._config_service.validate(config)
        if errors:
            QMessageBox.warning(self, "Invalid settings", "\n".join(errors))
            return
        self._preimport_worker = _PreImportWorker(
            self._preimport_service,
            config,
            mode="resume",
            job_id=self._active_preimport_job_id,
            parent=self,
        )
        self._preimport_worker.progress.connect(self._on_import_progress)  # type: ignore[arg-type]
        self._preimport_worker.preimport_done.connect(self._on_preimport_done)  # type: ignore[arg-type]
        self._status.setText("Retrying all failed items...")
        self.refresh_enabled_state()
        self._preimport_worker.start()

    def _format_summary(self, summary: ImportSummary) -> str:
        model_line = "Model check: not enabled"
        if summary.model_checked:
            state = "ready" if summary.model_ready else "not ready"
            detail = f" ({summary.model_message})" if summary.model_message else ""
            model_line = f"Model check: {state}{detail}"

        lines = [
            f"Processed: {summary.total}",
            f"Imported: {summary.imported}",
            f"Duplicates: {summary.duplicates}",
            f"Skipped (no time): {summary.skipped_no_capture_time}",
            f"Errors: {summary.errors}",
            model_line,
        ]
        if summary.aborted:
            lines.append(f"Aborted: {summary.abort_reason or 'yes'}")
        lines.extend(["", "Details:"])
        lines.extend(self._format_result(result) for result in summary.results)
        return "\n".join(lines)

    @staticmethod
    def _format_result(result: FileImportResult) -> str:
        rel = result.relative_path if result.relative_path else "-"
        tags = ", ".join(result.applied_tags) if result.applied_tags else "-"
        auto = ", ".join(result.autotags) if result.autotags else "-"
        reason = result.reason if result.reason else "-"
        return f"[{result.status}] {result.source} -> {rel} | tags={tags} | autotags={auto} | {reason}"

    def _populate_details(self, summary: ImportSummary, library_root: Path) -> None:
        self._suppress_cell_changed = True
        self._preimport_details_active = False
        self._latest_preimport_items = None
        self._retry_all_failed_button.setEnabled(False)
        self._details_table.clearContents()
        self._details_table.setRowCount(0)
        self._details_table.setColumnCount(7)
        self._details_table.setHorizontalHeaderLabels(
            ["Status", "Source", "Stored Path", "Tags", "Auto Tags", "Reason", "Actions"]
        )
        self._details_table.setColumnWidth(0, 130)
        self._details_table.setColumnWidth(1, 250)
        self._details_table.setColumnWidth(2, 200)
        self._details_table.setColumnWidth(3, 150)
        self._details_table.setColumnWidth(4, 180)
        self._details_table.setColumnWidth(5, 200)
        self._details_table.setColumnWidth(6, 280)
        self._details_table.setRowCount(len(summary.results))
        no_edit = ~Qt.ItemFlag.ItemIsEditable
        for row, result in enumerate(summary.results):
            status_item = QTableWidgetItem(result.status)
            status_item.setFlags(status_item.flags() & no_edit)
            source_item = QTableWidgetItem(result.source)
            source_item.setFlags(source_item.flags() & no_edit)
            stored_item = QTableWidgetItem(result.relative_path or "-")
            stored_item.setFlags(stored_item.flags() & no_edit)
            tags_item = QTableWidgetItem(", ".join(result.applied_tags) if result.applied_tags else "-")
            tags_item.setFlags(tags_item.flags() & no_edit)
            autotags_item = QTableWidgetItem(", ".join(result.autotags) if result.autotags else "-")
            autotags_item.setFlags(autotags_item.flags() & no_edit)
            reason_item = QTableWidgetItem(result.reason or "-")
            reason_item.setFlags(reason_item.flags() & no_edit)
            source_item.setToolTip(result.source)
            if result.relative_path:
                stored_item.setToolTip(result.relative_path)
            if result.autotags:
                autotags_item.setToolTip(", ".join(result.autotags))

            self._details_table.setItem(row, 0, status_item)
            self._details_table.setItem(row, 1, source_item)
            self._details_table.setItem(row, 2, stored_item)
            self._details_table.setItem(row, 3, tags_item)
            self._details_table.setItem(row, 4, autotags_item)
            self._details_table.setItem(row, 5, reason_item)

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
            self._details_table.setCellWidget(row, 6, actions)
        self._suppress_cell_changed = False

    def _add_folder_option(self, folder: Path) -> None:
        resolved = str(folder.resolve(strict=False))
        if self._folder_combo.findText(resolved) == -1:
            self._folder_combo.addItem(resolved)

    def _add_folder_tree_options(self, root: Path) -> None:
        self._add_folder_option(root)
        for path in root.rglob("*"):
            if path.is_dir():
                self._add_folder_option(path)

    def _add_source_paths(self, paths: list[Path]) -> None:
        seen = {
            str(Path(self._source_list.item(i).text()).resolve(strict=False))
            for i in range(self._source_list.count())
        }
        for path in paths:
            resolved = str(path.resolve(strict=False))
            if resolved in seen:
                continue
            seen.add(resolved)
            self._source_list.addItem(str(path))
            self._add_folder_option(path.parent)

    @staticmethod
    def _deduplicate_paths(paths: list[Path]) -> list[Path]:
        unique: list[Path] = []
        seen: set[str] = set()
        for path in paths:
            resolved = str(path.resolve(strict=False))
            if resolved in seen:
                continue
            seen.add(resolved)
            unique.append(path)
        return unique

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

    @staticmethod
    def _parse_capture_time(text: str) -> str | None:
        if not text.strip():
            return None
        try:
            parsed = datetime.fromisoformat(text.strip())
        except ValueError:
            return None
        return parsed.isoformat(sep=" ", timespec="seconds")

    def _apply_folder_rule(self) -> None:
        folder = self._folder_combo.currentText().strip()
        if not folder:
            QMessageBox.information(self, "Folder required", "Please add/select a folder first.")
            return
        tags = self._parse_tags(self._tags_input.text())
        capture_time = self._parse_capture_time(self._capture_time_input.text())
        if not tags and not capture_time:
            QMessageBox.information(
                self,
                "Rule required",
                "Please input at least one tag or a capture time fallback.",
            )
            return
        if self._capture_time_input.text().strip() and capture_time is None:
            QMessageBox.warning(
                self,
                "Invalid capture time",
                "Use format YYYY-MM-DD HH:MM:SS or ISO datetime.",
            )
            return
        existing = self._folder_tag_rules.get(folder, [])
        merged = existing + [t for t in tags if t not in existing]
        if merged:
            self._folder_tag_rules[folder] = merged
        elif folder in self._folder_tag_rules:
            self._folder_tag_rules.pop(folder, None)

        if capture_time:
            self._folder_capture_time_rules[folder] = capture_time
        elif not self._capture_time_input.text().strip():
            self._folder_capture_time_rules.pop(folder, None)

        self._refresh_rules_list()
        self._tags_input.clear()
        self._capture_time_input.clear()

    def _refresh_rules_list(self) -> None:
        self._rules_list.clear()
        keys = list(dict.fromkeys(list(self._folder_tag_rules.keys()) + list(self._folder_capture_time_rules.keys())))
        for key in keys:
            tags = self._folder_tag_rules.get(key, [])
            capture_time = self._folder_capture_time_rules.get(key)
            tags_text = ", ".join(tags) if tags else "-"
            capture_text = capture_time if capture_time else "-"
            item = QListWidgetItem(f"{key} -> tags: {tags_text} | fallback time: {capture_text}")
            item.setData(Qt.ItemDataRole.UserRole, key)
            self._rules_list.addItem(item)

    def _remove_selected_rule(self) -> None:
        item = self._rules_list.currentItem()
        if item is None:
            return
        key = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(key, str):
            self._folder_tag_rules.pop(key, None)
            self._folder_capture_time_rules.pop(key, None)
        self._refresh_rules_list()

    def _clear_rules(self) -> None:
        self._folder_tag_rules.clear()
        self._folder_capture_time_rules.clear()
        self._refresh_rules_list()

    def _collect_valid_folder_rules(self) -> tuple[dict[str, list[str]], dict[str, str]]:
        valid_tags: dict[str, list[str]] = {}
        valid_capture_times: dict[str, str] = {}
        skipped: list[str] = []
        keys = set(self._folder_tag_rules.keys()) | set(self._folder_capture_time_rules.keys())
        for folder in keys:
            folder_path = Path(folder)
            if not folder_path.exists() or not folder_path.is_dir():
                skipped.append(folder)
                continue
            resolved = str(folder_path.resolve(strict=False))
            tags = self._folder_tag_rules.get(folder, [])
            capture_time = self._folder_capture_time_rules.get(folder)
            if tags:
                valid_tags[resolved] = tags
            if capture_time:
                valid_capture_times[resolved] = capture_time
        if skipped:
            QMessageBox.warning(
                self,
                "Skipped invalid rules",
                "These folders are unavailable and were skipped:\n" + "\n".join(skipped),
            )
        return valid_tags, valid_capture_times

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

    def _refresh_preimport_status(self) -> None:
        if self._active_preimport_job_id is None:
            self._preimport_status.setText("Pre-import DB: no active job")
            return
        state = self._preimport_service.get_job_state(self._active_preimport_job_id)
        self._preimport_status.setText(
            f"Pre-import job {state.job_id[:8]} | status={state.status} | "
            f"planned={state.planned} prepared={state.prepared} failed={state.failed}"
        )

