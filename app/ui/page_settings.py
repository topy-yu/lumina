from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.services.ai_model_service import (
    AIModelProvider,
    ConnectionStatus,
    ModelInfo,
    get_provider,
)
from app.services.config_service import AppConfig, ConfigService
from app.services.db_check_service import CheckFileResult, DbCheckService, DbCheckSummary


class _DbCheckWorker(QThread):
    progress = Signal(str)
    check_done = Signal(object)

    def __init__(self, service: DbCheckService, config: AppConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._service = service
        self._config = config

    def run(self) -> None:
        result = self._service.check(self._config, progress=self.progress.emit)
        self.check_done.emit(result)


class _AICheckWorker(QThread):
    check_done = Signal(object)

    def __init__(self, provider: AIModelProvider, api_url: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._provider = provider
        self._api_url = api_url

    def run(self) -> None:
        status = self._provider.check_connection(self._api_url)
        self.check_done.emit(status)


class _AIModelListWorker(QThread):
    list_done = Signal(object)

    def __init__(self, provider: AIModelProvider, api_url: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._provider = provider
        self._api_url = api_url

    def run(self) -> None:
        models = self._provider.list_models(self._api_url)
        self.list_done.emit(models)


class _AIModelCheckWorker(QThread):
    check_done = Signal(object)

    def __init__(
        self, provider: AIModelProvider, api_url: str, model_name: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._provider = provider
        self._api_url = api_url
        self._model_name = model_name

    def run(self) -> None:
        status = self._provider.check_model(self._api_url, self._model_name)
        self.check_done.emit(status)


class _AIModelLoadWorker(QThread):
    load_done = Signal(bool, str)

    def __init__(
        self, provider: AIModelProvider, api_url: str, model_name: str,
        *, load: bool, parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._provider = provider
        self._api_url = api_url
        self._model_name = model_name
        self._load = load

    def run(self) -> None:
        if self._load:
            ok, msg = self._provider.load_model(self._api_url, self._model_name)
        else:
            ok, msg = self._provider.unload_model(self._api_url, self._model_name)
        self.load_done.emit(ok, msg)


class SettingsPage(QWidget):
    settings_saved = Signal()

    def __init__(
        self,
        config_service: ConfigService,
        db_check_service: DbCheckService,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._config_service = config_service
        self._db_check_service = db_check_service
        self._check_worker: _DbCheckWorker | None = None
        self._latest_summary: DbCheckSummary | None = None
        self._latest_library_root: Path | None = None
        self._ai_check_worker: _AICheckWorker | None = None
        self._ai_model_list_worker: _AIModelListWorker | None = None
        self._ai_model_check_worker: _AIModelCheckWorker | None = None
        self._ai_model_load_worker: _AIModelLoadWorker | None = None

        self._library_edit = QLineEdit()
        self._status_label = QLabel()
        self._check_btn = QPushButton("Check Database")

        self._check_log = QTextEdit()
        self._check_log.setReadOnly(True)
        self._check_log.setMaximumHeight(100)

        self._summary_text = QTextEdit()
        self._summary_text.setReadOnly(True)
        self._summary_text.setMaximumHeight(120)

        self._details_table = QTableWidget(0, 5)
        self._details_table.setHorizontalHeaderLabels(
            ["Status", "Path", "Old Path", "Reason", "Actions"]
        )
        self._details_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._details_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._details_table.verticalHeader().setVisible(False)
        self._details_table.horizontalHeader().setStretchLastSection(True)
        self._details_table.setColumnWidth(0, 90)
        self._details_table.setColumnWidth(1, 280)
        self._details_table.setColumnWidth(2, 250)
        self._details_table.setColumnWidth(3, 220)
        self._details_table.setColumnWidth(4, 100)

        self._provider_combo = QComboBox()
        self._provider_combo.addItems(["Ollama"])

        self._api_url_edit = QLineEdit()
        self._api_url_edit.setPlaceholderText("http://127.0.0.1:11434")

        self._model_combo = QComboBox()
        self._model_combo.setEditable(True)
        self._model_combo.setMinimumWidth(200)

        self._refresh_models_btn = QPushButton("Refresh Models")
        self._check_ai_btn = QPushButton("Check Connection")
        self._start_ai_btn = QPushButton("Start")
        self._stop_ai_btn = QPushButton("Stop")
        self._check_model_btn = QPushButton("Check Model")
        self._load_model_btn = QPushButton("Load Model")
        self._unload_model_btn = QPushButton("Unload Model")
        self._load_model_btn.setVisible(False)
        self._unload_model_btn.setVisible(False)

        self._ai_status_label = QLabel()
        self._ai_status_label.setTextFormat(Qt.TextFormat.RichText)

        self._model_status_label = QLabel()
        self._model_status_label.setTextFormat(Qt.TextFormat.RichText)

        self._build_ui()
        self.load_from_config()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        form = QFormLayout()

        lib_row = QHBoxLayout()
        lib_row.addWidget(self._library_edit)
        lib_pick = QPushButton("Browse...")
        lib_pick.clicked.connect(self._choose_library)  # type: ignore[arg-type]
        lib_row.addWidget(lib_pick)
        form.addRow("Photo library folder", lib_row)

        save_btn = QPushButton("Save settings")
        save_btn.clicked.connect(self.save_settings)  # type: ignore[arg-type]

        self._check_btn.clicked.connect(self._run_db_check)  # type: ignore[arg-type]

        ai_group = QGroupBox("AI Model Settings")
        ai_layout = QFormLayout()
        ai_layout.addRow("Provider:", self._provider_combo)
        ai_layout.addRow("API URL:", self._api_url_edit)

        model_row = QHBoxLayout()
        model_row.addWidget(self._model_combo, 1)
        model_row.addWidget(self._refresh_models_btn)
        ai_layout.addRow("Model:", model_row)

        server_row = QHBoxLayout()
        server_row.addWidget(self._ai_status_label, 1)
        server_row.addWidget(self._check_ai_btn)
        server_row.addWidget(self._start_ai_btn)
        server_row.addWidget(self._stop_ai_btn)
        ai_layout.addRow("Server:", server_row)

        model_status_row = QHBoxLayout()
        model_status_row.addWidget(self._model_status_label, 1)
        model_status_row.addWidget(self._check_model_btn)
        model_status_row.addWidget(self._load_model_btn)
        model_status_row.addWidget(self._unload_model_btn)
        ai_layout.addRow("Model Status:", model_status_row)

        ai_group.setLayout(ai_layout)

        self._check_ai_btn.clicked.connect(self._check_ai_connection)  # type: ignore[arg-type]
        self._refresh_models_btn.clicked.connect(self._refresh_ai_models)  # type: ignore[arg-type]
        self._start_ai_btn.clicked.connect(self._start_ai_server)  # type: ignore[arg-type]
        self._stop_ai_btn.clicked.connect(self._stop_ai_server)  # type: ignore[arg-type]
        self._check_model_btn.clicked.connect(self._check_ai_model)  # type: ignore[arg-type]
        self._load_model_btn.clicked.connect(self._load_ai_model)  # type: ignore[arg-type]
        self._unload_model_btn.clicked.connect(self._unload_ai_model)  # type: ignore[arg-type]
        self._provider_combo.currentTextChanged.connect(self._on_provider_changed)  # type: ignore[arg-type]
        self._model_combo.currentIndexChanged.connect(self._on_model_selection_changed)  # type: ignore[arg-type]

        root.addLayout(form)
        root.addWidget(ai_group)
        root.addWidget(save_btn)
        root.addWidget(self._status_label)
        root.addWidget(self._check_btn)
        root.addWidget(self._check_log)
        root.addWidget(self._summary_text)
        root.addWidget(self._details_table)

    def _choose_library(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Select photo library root")
        if folder:
            self._library_edit.setText(folder)

    def load_from_config(self) -> None:
        config = self._config_service.normalize(self._config_service.load())
        self._library_edit.setText(config.library_root)

        idx = self._provider_combo.findText(config.ai_provider.title())
        if idx >= 0:
            self._provider_combo.setCurrentIndex(idx)
        self._api_url_edit.setText(config.ai_api_url)
        if config.ai_model_name:
            self._model_combo.setEditText(config.ai_model_name)

        self._status_label.setText("Ready.")
        self._refresh_check_button()

        if config.ai_api_url:
            self._check_ai_connection()

    def current_config(self) -> AppConfig:
        return self._config_service.normalize(
            AppConfig(
                library_root=self._library_edit.text().strip(),
                db_path="",
                ai_provider=self._provider_combo.currentText().lower(),
                ai_api_url=self._api_url_edit.text().strip(),
                ai_model_name=self._model_combo.currentText(),
            )
        )

    def save_settings(self) -> None:
        config = self.current_config()
        errors = self._config_service.validate(config)
        if errors:
            QMessageBox.warning(self, "Invalid settings", "\n".join(errors))
            self._status_label.setText("Settings are invalid.")
            return
        self._config_service.save(config)
        self._status_label.setText("Settings saved.")
        self._refresh_check_button()
        self.settings_saved.emit()

    def _refresh_check_button(self) -> None:
        config = self._config_service.load()
        errors = self._config_service.validate(config)
        self._check_btn.setEnabled(len(errors) == 0 and self._check_worker is None)

    # ── AI Model ──────────────────────────────────────────────

    def _get_ai_provider(self) -> AIModelProvider:
        return get_provider(self._provider_combo.currentText().lower())

    def _check_ai_connection(self) -> None:
        if self._ai_check_worker is not None:
            return
        self._ai_status_label.setText('<span style="color: orange;">● Checking...</span>')
        self._check_ai_btn.setEnabled(False)

        provider = self._get_ai_provider()
        api_url = self._api_url_edit.text().strip() or "http://127.0.0.1:11434"

        self._ai_check_worker = _AICheckWorker(provider, api_url, self)
        self._ai_check_worker.check_done.connect(self._on_ai_check_done)  # type: ignore[arg-type]
        self._ai_check_worker.start()

    def _on_ai_check_done(self, result: object) -> None:
        assert isinstance(result, ConnectionStatus)
        self._check_ai_btn.setEnabled(True)
        self._ai_check_worker = None

        if result.connected:
            self._ai_status_label.setText('<span style="color: green;">● Connected</span>')
            self._start_ai_btn.setEnabled(False)
            self._stop_ai_btn.setEnabled(True)
            self._refresh_ai_models()
        else:
            msg = result.message.replace("<", "&lt;").replace(">", "&gt;")
            self._ai_status_label.setText(
                f'<span style="color: red;">● Disconnected</span>'
                f'<br/><span style="color: gray; font-size: small;">{msg}</span>'
            )
            self._model_status_label.setText(
                '<span style="color: gray;">● Server not connected</span>'
            )
            self._load_model_btn.setVisible(False)
            self._unload_model_btn.setVisible(False)
            self._start_ai_btn.setEnabled(True)
            self._stop_ai_btn.setEnabled(False)

    def _refresh_ai_models(self) -> None:
        if self._ai_model_list_worker is not None:
            return
        self._refresh_models_btn.setEnabled(False)

        provider = self._get_ai_provider()
        api_url = self._api_url_edit.text().strip() or "http://127.0.0.1:11434"

        self._ai_model_list_worker = _AIModelListWorker(provider, api_url, self)
        self._ai_model_list_worker.list_done.connect(self._on_ai_model_list_done)  # type: ignore[arg-type]
        self._ai_model_list_worker.start()

    def _on_ai_model_list_done(self, result: object) -> None:
        assert isinstance(result, list)
        self._refresh_models_btn.setEnabled(True)
        self._ai_model_list_worker = None

        current_model = self._model_combo.currentText()
        self._model_combo.clear()

        for model_info in result:
            self._model_combo.addItem(model_info.name)

        idx = self._model_combo.findText(current_model)
        if idx >= 0:
            self._model_combo.setCurrentIndex(idx)
        elif current_model:
            self._model_combo.setEditText(current_model)

        if self._model_combo.currentText():
            self._check_ai_model()

    def _check_ai_model(self) -> None:
        if self._ai_model_check_worker is not None:
            return
        model_name = self._model_combo.currentText().strip()
        if not model_name:
            self._model_status_label.setText(
                '<span style="color: gray;">● No model selected</span>'
            )
            return

        self._model_status_label.setText(
            '<span style="color: orange;">● Checking...</span>'
        )
        self._check_model_btn.setEnabled(False)

        provider = self._get_ai_provider()
        api_url = self._api_url_edit.text().strip() or "http://127.0.0.1:11434"

        self._ai_model_check_worker = _AIModelCheckWorker(
            provider, api_url, model_name, self,
        )
        self._ai_model_check_worker.check_done.connect(self._on_ai_model_check_done)  # type: ignore[arg-type]
        self._ai_model_check_worker.start()

    def _on_ai_model_check_done(self, result: object) -> None:
        assert isinstance(result, ConnectionStatus)
        self._check_model_btn.setEnabled(True)
        self._ai_model_check_worker = None

        if result.connected and result.loaded:
            msg = result.message.replace("<", "&lt;").replace(">", "&gt;")
            self._model_status_label.setText(
                f'<span style="color: green;">● {msg}</span>'
            )
            self._load_model_btn.setVisible(False)
            self._unload_model_btn.setVisible(True)
            self._unload_model_btn.setEnabled(True)
        elif result.connected:
            msg = result.message.replace("<", "&lt;").replace(">", "&gt;")
            self._model_status_label.setText(
                f'<span style="color: #cc8800;">● {msg}</span>'
            )
            self._load_model_btn.setVisible(True)
            self._load_model_btn.setEnabled(True)
            self._unload_model_btn.setVisible(False)
        else:
            msg = result.message.replace("<", "&lt;").replace(">", "&gt;")
            self._model_status_label.setText(
                f'<span style="color: red;">● {msg}</span>'
            )
            self._load_model_btn.setVisible(False)
            self._unload_model_btn.setVisible(False)

    def _on_model_selection_changed(self, _index: int) -> None:
        self._load_model_btn.setVisible(False)
        self._unload_model_btn.setVisible(False)
        self._check_ai_model()

    def _load_ai_model(self) -> None:
        if self._ai_model_load_worker is not None:
            return
        model_name = self._model_combo.currentText().strip()
        if not model_name:
            return
        self._model_status_label.setText(
            '<span style="color: orange;">● Loading model (this may take a while)...</span>'
        )
        self._load_model_btn.setEnabled(False)
        self._check_model_btn.setEnabled(False)

        provider = self._get_ai_provider()
        api_url = self._api_url_edit.text().strip() or "http://127.0.0.1:11434"

        self._ai_model_load_worker = _AIModelLoadWorker(
            provider, api_url, model_name, load=True, parent=self,
        )
        self._ai_model_load_worker.load_done.connect(self._on_model_load_done)  # type: ignore[arg-type]
        self._ai_model_load_worker.start()

    def _unload_ai_model(self) -> None:
        if self._ai_model_load_worker is not None:
            return
        model_name = self._model_combo.currentText().strip()
        if not model_name:
            return
        self._model_status_label.setText(
            '<span style="color: orange;">● Unloading model...</span>'
        )
        self._unload_model_btn.setEnabled(False)
        self._check_model_btn.setEnabled(False)

        provider = self._get_ai_provider()
        api_url = self._api_url_edit.text().strip() or "http://127.0.0.1:11434"

        self._ai_model_load_worker = _AIModelLoadWorker(
            provider, api_url, model_name, load=False, parent=self,
        )
        self._ai_model_load_worker.load_done.connect(self._on_model_load_done)  # type: ignore[arg-type]
        self._ai_model_load_worker.start()

    def _on_model_load_done(self, success: bool, message: str) -> None:
        self._ai_model_load_worker = None
        self._check_model_btn.setEnabled(True)

        if success:
            self._check_ai_model()
        else:
            msg = message.replace("<", "&lt;").replace(">", "&gt;")
            self._model_status_label.setText(
                f'<span style="color: red;">● {msg}</span>'
            )
            self._load_model_btn.setEnabled(True)
            self._unload_model_btn.setEnabled(True)

    def _start_ai_server(self) -> None:
        provider = self._get_ai_provider()
        success, message = provider.start_server()
        if success:
            self._ai_status_label.setText(f'<span style="color: orange;">● {message}</span>')
            self._start_ai_btn.setEnabled(False)
            QTimer.singleShot(3000, self._check_ai_connection)
        else:
            QMessageBox.warning(self, "Start Failed", message)

    def _stop_ai_server(self) -> None:
        provider = self._get_ai_provider()
        success, message = provider.stop_server()
        if success:
            self._ai_status_label.setText('<span style="color: orange;">● Stopping...</span>')
            self._stop_ai_btn.setEnabled(False)
            QTimer.singleShot(1500, self._check_ai_connection)
        else:
            QMessageBox.warning(self, "Stop Failed", message)

    def _on_provider_changed(self, _text: str) -> None:
        try:
            provider = self._get_ai_provider()
        except ValueError:
            return
        show_server = provider.supports_local_server
        self._start_ai_btn.setVisible(show_server)
        self._stop_ai_btn.setVisible(show_server)

    # ── DB check ──────────────────────────────────────────────

    def _run_db_check(self) -> None:
        config = self._config_service.load()
        errors = self._config_service.validate(config)
        if errors:
            QMessageBox.warning(self, "Invalid settings", "\n".join(errors))
            return

        self._check_btn.setEnabled(False)
        self._check_log.clear()
        self._summary_text.clear()
        self._details_table.setRowCount(0)
        self._status_label.setText("Running database check...")

        self._latest_library_root = Path(config.library_root)
        self._check_worker = _DbCheckWorker(self._db_check_service, config, self)
        self._check_worker.progress.connect(self._on_check_progress)  # type: ignore[arg-type]
        self._check_worker.check_done.connect(self._on_check_done)  # type: ignore[arg-type]
        self._check_worker.start()

    def _on_check_progress(self, message: str) -> None:
        self._check_log.append(message)

    def _on_check_done(self, result: object) -> None:
        assert isinstance(result, DbCheckSummary)
        self._latest_summary = result

        self._summary_text.setPlainText(self._format_summary(result))
        self._populate_details(result)
        self._check_worker = None
        self._check_btn.setEnabled(True)
        self._status_label.setText("Database check complete.")

    # ── Summary / details ─────────────────────────────────────

    @staticmethod
    def _format_summary(summary: DbCheckSummary) -> str:
        lines = [
            f"Files on disk:    {summary.total_on_disk}",
            f"Records in DB:    {summary.total_in_db}",
            "",
            f"Added:    {summary.added}",
            f"Moved:    {summary.moved}",
            f"Deleted:  {summary.deleted}",
            f"Errors:   {summary.errors}",
        ]
        return "\n".join(lines)

    def _populate_details(self, summary: DbCheckSummary) -> None:
        self._details_table.setRowCount(len(summary.results))
        for row, result in enumerate(summary.results):
            self._details_table.setItem(row, 0, QTableWidgetItem(result.status))
            self._details_table.setItem(row, 1, QTableWidgetItem(result.relative_path))
            self._details_table.setItem(row, 2, QTableWidgetItem(result.old_path or "-"))
            self._details_table.setItem(row, 3, QTableWidgetItem(result.reason or "-"))

            actions = QWidget()
            actions_layout = QHBoxLayout(actions)
            actions_layout.setContentsMargins(0, 0, 0, 0)

            preview_btn = QPushButton("Preview")
            preview_btn.clicked.connect(  # type: ignore[arg-type]
                lambda _checked=False, r=row: self._preview_row(r)
            )

            if not self._can_preview(result):
                preview_btn.setEnabled(False)

            actions_layout.addWidget(preview_btn)
            self._details_table.setCellWidget(row, 4, actions)

    def _can_preview(self, result: CheckFileResult) -> bool:
        if self._latest_library_root is None:
            return False
        if result.status == "deleted":
            return False
        file_path = self._latest_library_root / result.relative_path
        return file_path.exists() and file_path.is_file()

    def _preview_row(self, row: int) -> None:
        if self._latest_summary is None or self._latest_library_root is None:
            return
        result = self._latest_summary.results[row]
        file_path = self._latest_library_root / result.relative_path
        if not file_path.exists() or not file_path.is_file():
            QMessageBox.information(self, "File missing", "File is no longer available.")
            return
        self._show_preview(file_path)

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
