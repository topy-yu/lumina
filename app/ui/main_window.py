from __future__ import annotations

from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app.services.config_service import ConfigService
from app.services.file_service import FileService
from app.services.metadata_service import MetadataService
from app.services.photo_import_service import PhotoImportService
from app.db.repository import PhotoRepository
from app.ui.page_import import ImportPage
from app.ui.page_placeholder import PlaceholderPage
from app.ui.page_settings import SettingsPage


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Lumina")
        self.resize(1000, 700)

        self._config_service = ConfigService()
        self._repository = PhotoRepository()
        self._metadata_service = MetadataService()
        self._file_service = FileService()
        self._import_service = PhotoImportService(
            repository=self._repository,
            metadata_service=self._metadata_service,
            file_service=self._file_service,
        )

        self._settings_page = SettingsPage(self._config_service)
        self._import_page = ImportPage(self._config_service, self._import_service)
        self._settings_page.settings_saved.connect(self._import_page.refresh_enabled_state)  # type: ignore[arg-type]

        self._build_ui()

    def _build_ui(self) -> None:
        container = QWidget(self)
        root = QHBoxLayout(container)
        self.setCentralWidget(container)

        nav = QListWidget()
        nav.setMaximumWidth(220)
        nav_items = ["1. Settings", "2. Import", "3. Tagging", "4. Search"]
        for item in nav_items:
            nav.addItem(QListWidgetItem(item))

        stack = QStackedWidget()
        stack.addWidget(self._settings_page)
        stack.addWidget(self._import_page)
        stack.addWidget(PlaceholderPage("Page 3: Tagging"))
        stack.addWidget(PlaceholderPage("Page 4: Search"))

        nav.currentRowChanged.connect(stack.setCurrentIndex)  # type: ignore[arg-type]
        nav.currentRowChanged.connect(self._on_nav_change)  # type: ignore[arg-type]
        nav.setCurrentRow(0)

        right = QVBoxLayout()
        title = QLabel("Lumina")
        title.setStyleSheet("font-size: 22px; font-weight: bold;")
        right.addWidget(title)
        right.addWidget(stack)

        root.addWidget(nav)
        root.addLayout(right)

    def _on_nav_change(self, index: int) -> None:
        if index == 1:
            self._import_page.refresh_enabled_state()

