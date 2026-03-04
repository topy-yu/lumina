from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.services.config_service import AppConfig, ConfigService


class SettingsPage(QWidget):
    settings_saved = Signal()

    def __init__(self, config_service: ConfigService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._config_service = config_service

        self._library_edit = QLineEdit()
        self._db_edit = QLineEdit()
        self._status_label = QLabel()

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

        db_row = QHBoxLayout()
        db_row.addWidget(self._db_edit)
        db_pick = QPushButton("Browse...")
        db_pick.clicked.connect(self._choose_db_file)  # type: ignore[arg-type]
        db_row.addWidget(db_pick)
        form.addRow("Database file", db_row)

        save_btn = QPushButton("Save settings")
        save_btn.clicked.connect(self.save_settings)  # type: ignore[arg-type]

        root.addLayout(form)
        root.addWidget(save_btn)
        root.addWidget(self._status_label)
        root.addStretch(1)

    def _choose_library(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Select photo library root")
        if folder:
            self._library_edit.setText(folder)

    def _choose_db_file(self) -> None:
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "Select SQLite database file",
            filter="SQLite Database (*.db *.sqlite);;All Files (*)",
        )
        if file_path:
            self._db_edit.setText(file_path)

    def load_from_config(self) -> None:
        config = self._config_service.load()
        self._library_edit.setText(config.library_root)
        self._db_edit.setText(config.db_path)
        self._status_label.setText("Ready.")

    def current_config(self) -> AppConfig:
        return AppConfig(
            library_root=self._library_edit.text().strip(),
            db_path=self._db_edit.text().strip(),
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
        self.settings_saved.emit()

