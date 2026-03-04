from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMessageBox,
    QPushButton,
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

        self._source_list = QListWidget()
        self._summary = QTextEdit()
        self._summary.setReadOnly(True)
        self._status = QLabel("Add files or folders to begin.")

        self._import_button = QPushButton("Import")
        self._import_button.clicked.connect(self._run_import)  # type: ignore[arg-type]

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

        controls.addWidget(add_files_btn)
        controls.addWidget(add_dir_btn)
        controls.addWidget(clear_btn)
        controls.addWidget(self._import_button)

        layout.addLayout(controls)
        layout.addWidget(self._source_list)
        layout.addWidget(self._status)
        layout.addWidget(self._summary)

    def refresh_enabled_state(self) -> None:
        config = self._config_service.load()
        errors = self._config_service.validate(config)
        enabled = len(errors) == 0
        self._import_button.setEnabled(enabled)
        if not enabled:
            self._status.setText("Configure valid library and DB paths on page 1.")
        else:
            self._status.setText("Ready to import.")

    def _add_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Select photos",
            filter="Images (*.jpg *.jpeg *.png *.webp *.heic *.tif *.tiff *.bmp);;All Files (*)",
        )
        for file in files:
            self._source_list.addItem(file)

    def _add_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Select folder to scan for photos")
        if not folder:
            return
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

        summary = self._import_service.import_files(file_paths, config)
        self._summary.setPlainText(self._format_summary(summary))
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
        reason = result.reason if result.reason else "-"
        return f"[{result.status}] {result.source} -> {rel} | {reason}"

