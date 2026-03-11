from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

logger = logging.getLogger("lumina.ui.tagging")
from PySide6.QtWidgets import (
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.db.repository import PhotoRecord, PhotoRepository
from app.services.ai_model_service import get_provider
from app.services.config_service import ConfigService
from app.services.vision_service import VisionService
from app.ui.widgets import ChipSelector

_TAG_SPLIT_RE = re.compile(r"[,，;；]")


# ── Worker signals / base ──────────────────────────────────────


class _WorkerSignals(QObject):
    progress = Signal(int, int)
    log = Signal(str)
    finished = Signal()
    error = Signal(str)


# ── Feature 1: batch match tags ───────────────────────────────


class _MatchTagsWorker(QThread):
    def __init__(
        self,
        photos: list[PhotoRecord],
        candidate_tags: list[str],
        library_root: Path,
        db_path: Path,
        api_url: str,
        model_name: str,
        vision_service: VisionService,
        repository: PhotoRepository,
    ) -> None:
        super().__init__()
        self.sig = _WorkerSignals()
        self._photos = photos
        self._candidate_tags = candidate_tags
        self._library_root = library_root
        self._db_path = db_path
        self._api_url = api_url
        self._model_name = model_name
        self._vision = vision_service
        self._repo = repository
        self._abort = False

    def abort(self) -> None:
        self._abort = True

    def run(self) -> None:
        total = len(self._photos)
        logger.info("Match tags worker started: %d photos, %d candidates", total, len(self._candidate_tags))
        tagged_count = 0
        skipped_count = 0
        candidate_lower = {t.lower() for t in self._candidate_tags}
        try:
            for i, photo in enumerate(self._photos):
                if self._abort:
                    self.sig.log.emit("Aborted.")
                    logger.info("Match tags worker aborted")
                    break
                image_path = self._library_root / photo.relative_path
                if not image_path.exists():
                    self.sig.log.emit(f"[{i+1}/{total}] SKIP (missing): {photo.relative_path}")
                    self.sig.progress.emit(i + 1, total)
                    continue

                existing = self._load_tags(photo.tags)
                existing_lower = {t.lower() for t in existing}
                if candidate_lower <= existing_lower:
                    skipped_count += 1
                    self.sig.log.emit(f"[{i+1}/{total}] SKIP (all tags present): {photo.relative_path}")
                    self.sig.progress.emit(i + 1, total)
                    continue

                matched = self._vision.match_tags(
                    image_path, self._api_url, self._model_name, self._candidate_tags,
                    strict=True,
                )
                if matched:
                    merged = list(dict.fromkeys(existing + matched))
                    tags_json = json.dumps(merged, ensure_ascii=False)
                    self._repo.update_tags_by_md5(self._db_path, photo.md5, tags_json)
                    tagged_count += 1
                    self.sig.log.emit(
                        f"[{i+1}/{total}] +{len(matched)} tags -> {photo.relative_path}  ({', '.join(matched)})"
                    )
                else:
                    self.sig.log.emit(f"[{i+1}/{total}] no match: {photo.relative_path}")
                self.sig.progress.emit(i + 1, total)
        except Exception as exc:  # noqa: BLE001
            logger.error("Match tags worker error: %s", exc)
            self.sig.error.emit(str(exc))
            return
        logger.info("Match tags worker finished: %d/%d tagged, %d skipped", tagged_count, total, skipped_count)
        self.sig.log.emit(f"Done. {tagged_count}/{total} photos tagged, {skipped_count} skipped.")
        self.sig.finished.emit()

    @staticmethod
    def _load_tags(raw: str) -> list[str]:
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, list) else []
        except (json.JSONDecodeError, TypeError):
            return []


# ── Feature 2: recalculate autotags ───────────────────────────


class _AutotagsWorker(QThread):
    def __init__(
        self,
        photos: list[PhotoRecord],
        library_root: Path,
        db_path: Path,
        api_url: str,
        model_name: str,
        vision_service: VisionService,
        repository: PhotoRepository,
    ) -> None:
        super().__init__()
        self.sig = _WorkerSignals()
        self._photos = photos
        self._library_root = library_root
        self._db_path = db_path
        self._api_url = api_url
        self._model_name = model_name
        self._vision = vision_service
        self._repo = repository
        self._abort = False

    def abort(self) -> None:
        self._abort = True

    def run(self) -> None:
        total = len(self._photos)
        logger.info("Autotags worker started: %d photos", total)
        updated_count = 0
        try:
            for i, photo in enumerate(self._photos):
                if self._abort:
                    self.sig.log.emit("Aborted.")
                    logger.info("Autotags worker aborted")
                    break
                image_path = self._library_root / photo.relative_path
                if not image_path.exists():
                    self.sig.log.emit(f"[{i+1}/{total}] SKIP (missing): {photo.relative_path}")
                    self.sig.progress.emit(i + 1, total)
                    continue

                autotags = self._vision.generate_autotags(
                    image_path, self._api_url, self._model_name, strict=True,
                )
                autotags_json = json.dumps(autotags, ensure_ascii=False)
                self._repo.update_autotags_by_md5(self._db_path, photo.md5, autotags_json)
                updated_count += 1
                tag_preview = ", ".join(autotags[:5])
                if len(autotags) > 5:
                    tag_preview += f" ... (+{len(autotags) - 5})"
                self.sig.log.emit(f"[{i+1}/{total}] {photo.relative_path}  [{tag_preview}]")
                self.sig.progress.emit(i + 1, total)
        except Exception as exc:  # noqa: BLE001
            logger.error("Autotags worker error: %s", exc)
            self.sig.error.emit(str(exc))
            return
        logger.info("Autotags worker finished: %d/%d updated", updated_count, total)
        self.sig.log.emit(f"Done. {updated_count}/{total} photos updated.")
        self.sig.finished.emit()


# ── Feature 3: person name tagging ────────────────────────────


class _PersonTagWorker(QThread):
    def __init__(
        self,
        photos: list[PhotoRecord],
        reference_path: Path,
        person_tag: str,
        library_root: Path,
        db_path: Path,
        api_url: str,
        model_name: str,
        vision_service: VisionService,
        repository: PhotoRepository,
    ) -> None:
        super().__init__()
        self.sig = _WorkerSignals()
        self._photos = photos
        self._reference_path = reference_path
        self._person_tag = person_tag
        self._library_root = library_root
        self._db_path = db_path
        self._api_url = api_url
        self._model_name = model_name
        self._vision = vision_service
        self._repo = repository
        self._abort = False

    def abort(self) -> None:
        self._abort = True

    def run(self) -> None:
        total = len(self._photos)
        matched_count = 0
        try:
            for i, photo in enumerate(self._photos):
                if self._abort:
                    self.sig.log.emit("Aborted.")
                    break
                image_path = self._library_root / photo.relative_path
                if not image_path.exists():
                    self.sig.log.emit(f"[{i+1}/{total}] SKIP (missing): {photo.relative_path}")
                    self.sig.progress.emit(i + 1, total)
                    continue

                is_match = self._vision.match_person(
                    self._reference_path, image_path, self._api_url, self._model_name,
                    strict=True,
                )
                if is_match:
                    existing = self._load_tags(photo.tags)
                    if self._person_tag not in existing:
                        existing.append(self._person_tag)
                        tags_json = json.dumps(existing, ensure_ascii=False)
                        self._repo.update_tags_by_md5(self._db_path, photo.md5, tags_json)
                    matched_count += 1
                    self.sig.log.emit(f"[{i+1}/{total}] MATCH: {photo.relative_path}")
                else:
                    self.sig.log.emit(f"[{i+1}/{total}] no match: {photo.relative_path}")
                self.sig.progress.emit(i + 1, total)
        except Exception as exc:  # noqa: BLE001
            logger.error("Person tag worker error: %s", exc)
            self.sig.error.emit(str(exc))
            return
        logger.info("Person tag worker finished: %d/%d matched '%s'", matched_count, total, self._person_tag)
        self.sig.log.emit(f"Done. {matched_count}/{total} photos matched '{self._person_tag}'.")
        self.sig.finished.emit()

    @staticmethod
    def _load_tags(raw: str) -> list[str]:
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, list) else []
        except (json.JSONDecodeError, TypeError):
            return []


# ── Tagging page ───────────────────────────────────────────────


class TaggingPage(QWidget):
    def __init__(
        self,
        config_service: ConfigService,
        repository: PhotoRepository,
        vision_service: VisionService,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._config_service = config_service
        self._repository = repository
        self._vision_service = vision_service
        self._library_root: Path | None = None
        self._db_path: Path | None = None
        self._ai_api_url: str = ""
        self._ai_model_name: str = ""
        self._reference_photo_path: Path | None = None
        self._worker: QThread | None = None

        self._build_ui()

    # ── UI construction ────────────────────────────────────────

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        # --- Folder selection ---
        folder_group = QGroupBox("Scope (directory selection)")
        folder_layout = QVBoxLayout(folder_group)
        folder_row = QHBoxLayout()

        level1_col = QVBoxLayout()
        level1_col.addWidget(QLabel("First-level directory:"))
        self._level1_selector = ChipSelector("Select first-level...")
        self._level1_selector.selection_changed.connect(self._on_level1_changed)  # type: ignore[arg-type]
        level1_col.addWidget(self._level1_selector)

        level2_col = QVBoxLayout()
        level2_col.addWidget(QLabel("Second-level directory:"))
        self._level2_selector = ChipSelector("Select second-level...")
        level2_col.addWidget(self._level2_selector)

        folder_row.addLayout(level1_col)
        folder_row.addLayout(level2_col)
        folder_layout.addLayout(folder_row)
        layout.addWidget(folder_group)

        # --- Feature 1: Batch add tags (model-assisted) ---
        f1_group = QGroupBox("Feature 1: Batch Add Tags (model-assisted)")
        f1_layout = QHBoxLayout(f1_group)
        f1_layout.addWidget(QLabel("Tags:"))
        self._f1_tags_input = QLineEdit()
        self._f1_tags_input.setPlaceholderText("tag1, tag2, tag3 ...")
        f1_layout.addWidget(self._f1_tags_input)
        self._f1_run_btn = QPushButton("Run")
        self._f1_run_btn.clicked.connect(self._run_match_tags)  # type: ignore[arg-type]
        f1_layout.addWidget(self._f1_run_btn)
        layout.addWidget(f1_group)

        # --- Feature 2: Recalculate autotags ---
        f2_group = QGroupBox("Feature 2: Recalculate Autotags")
        f2_layout = QHBoxLayout(f2_group)
        f2_layout.addWidget(QLabel("Regenerate autotags for all photos in scope."))
        f2_layout.addStretch()
        self._f2_run_btn = QPushButton("Run")
        self._f2_run_btn.clicked.connect(self._run_autotags)  # type: ignore[arg-type]
        f2_layout.addWidget(self._f2_run_btn)
        layout.addWidget(f2_group)

        # --- Feature 3: Tag person name ---
        f3_group = QGroupBox("Feature 3: Tag Person Name")
        f3_layout = QVBoxLayout(f3_group)

        ref_row = QHBoxLayout()
        ref_row.addWidget(QLabel("Reference photo:"))
        self._f3_browse_btn = QPushButton("Browse...")
        self._f3_browse_btn.clicked.connect(self._browse_reference)  # type: ignore[arg-type]
        ref_row.addWidget(self._f3_browse_btn)
        self._f3_ref_label = QLabel("(none)")
        self._f3_ref_label.setStyleSheet("color: #888;")
        ref_row.addWidget(self._f3_ref_label, stretch=1)
        f3_layout.addLayout(ref_row)

        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Person name tag:"))
        self._f3_name_input = QLineEdit()
        self._f3_name_input.setPlaceholderText("e.g. Alice")
        name_row.addWidget(self._f3_name_input)
        self._f3_run_btn = QPushButton("Run")
        self._f3_run_btn.clicked.connect(self._run_person_tag)  # type: ignore[arg-type]
        name_row.addWidget(self._f3_run_btn)
        f3_layout.addLayout(name_row)
        layout.addWidget(f3_group)

        # --- Progress ---
        progress_row = QHBoxLayout()
        self._progress_bar = QProgressBar()
        self._progress_bar.setValue(0)
        progress_row.addWidget(self._progress_bar)
        self._abort_btn = QPushButton("Abort")
        self._abort_btn.setEnabled(False)
        self._abort_btn.clicked.connect(self._abort_worker)  # type: ignore[arg-type]
        progress_row.addWidget(self._abort_btn)
        layout.addLayout(progress_row)

        self._status_label = QLabel("Ready")
        layout.addWidget(self._status_label)

        # --- Log area ---
        self._log_area = QTextEdit()
        self._log_area.setReadOnly(True)
        self._log_area.setMaximumHeight(200)
        layout.addWidget(self._log_area, stretch=1)

    # ── Page activation ────────────────────────────────────────

    def refresh(self) -> None:
        config = self._config_service.load()
        errors = self._config_service.validate(config)
        if errors:
            self._level1_selector.clear_all()
            self._level2_selector.clear_all()
            self._library_root = None
            self._db_path = None
            self._ai_api_url = ""
            self._ai_model_name = ""
            return

        self._library_root = Path(config.library_root)
        self._db_path = Path(config.db_path)
        self._ai_api_url = config.ai_api_url
        self._ai_model_name = config.ai_model_name

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

    # ── Scope helper ───────────────────────────────────────────

    def _get_scoped_photos(self) -> list[PhotoRecord] | None:
        """Return photos filtered by selected directories, or None on error."""
        if self._library_root is None or self._db_path is None:
            QMessageBox.warning(self, "Not ready", "Configure library settings first.")
            return None
        if not self._db_path.exists():
            QMessageBox.information(self, "No database", "Database not found. Import photos first.")
            return None

        selected_level1 = self._level1_selector.selected()
        selected_level2 = self._level2_selector.selected()

        allowed_prefixes: list[str] = []
        if selected_level2:
            for path in selected_level2:
                allowed_prefixes.append(f"{path}/")
        elif selected_level1:
            for l1 in selected_level1:
                allowed_prefixes.append(f"{l1}/")

        all_photos = self._repository.get_all_photos(self._db_path)

        if allowed_prefixes:
            all_photos = [
                p for p in all_photos
                if any(
                    p.relative_path.replace("\\", "/").startswith(prefix)
                    for prefix in allowed_prefixes
                )
            ]

        if not all_photos:
            QMessageBox.information(self, "No photos", "No photos found in the selected scope.")
            return None

        return all_photos

    def _check_ai_ready(self) -> bool:
        if not self._ai_api_url or not self._ai_model_name:
            QMessageBox.warning(
                self, "AI not configured",
                "Please configure AI provider and model in Settings first.",
            )
            return False
        try:
            config = self._config_service.load()
            provider = get_provider(config.ai_provider)
            status = provider.check_model(self._ai_api_url, self._ai_model_name)
            if not status.connected:
                QMessageBox.warning(
                    self, "AI not available",
                    f"Cannot reach AI service: {status.message}",
                )
                return False
            if not status.loaded:
                QMessageBox.warning(
                    self, "Model not loaded",
                    f"{status.message}\nPlease load the model in Settings first.",
                )
                return False
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "AI check failed", str(exc))
            return False
        return True

    # ── Job lifecycle ──────────────────────────────────────────

    def _set_running(self, running: bool) -> None:
        self._f1_run_btn.setEnabled(not running)
        self._f2_run_btn.setEnabled(not running)
        self._f3_run_btn.setEnabled(not running)
        self._abort_btn.setEnabled(running)
        if running:
            self._progress_bar.setValue(0)
            self._log_area.clear()

    def _on_progress(self, current: int, total: int) -> None:
        self._progress_bar.setMaximum(total)
        self._progress_bar.setValue(current)
        self._status_label.setText(f"Processing {current}/{total} ...")

    def _on_log(self, message: str) -> None:
        self._log_area.append(message)

    def _on_finished(self) -> None:
        self._set_running(False)
        self._status_label.setText("Finished")
        self._worker = None

    def _on_error(self, message: str) -> None:
        self._set_running(False)
        self._status_label.setText("Error")
        self._worker = None
        QMessageBox.warning(self, "Error", message)

    def _abort_worker(self) -> None:
        if self._worker is not None and hasattr(self._worker, "abort"):
            self._worker.abort()  # type: ignore[attr-defined]
            self._status_label.setText("Aborting...")

    def _connect_worker(self, worker: QThread) -> None:
        sigs: _WorkerSignals = worker.sig  # type: ignore[attr-defined]
        sigs.progress.connect(self._on_progress)  # type: ignore[arg-type]
        sigs.log.connect(self._on_log)  # type: ignore[arg-type]
        sigs.finished.connect(self._on_finished)  # type: ignore[arg-type]
        sigs.error.connect(self._on_error)  # type: ignore[arg-type]

    # ── Feature 1: run match tags ──────────────────────────────

    def _run_match_tags(self) -> None:
        if not self._check_ai_ready():
            return
        raw = self._f1_tags_input.text().strip()
        if not raw:
            QMessageBox.warning(self, "No tags", "Please enter at least one tag.")
            return
        candidate_tags = [t.strip() for t in _TAG_SPLIT_RE.split(raw) if t.strip()]
        if not candidate_tags:
            QMessageBox.warning(self, "No tags", "Please enter at least one tag.")
            return

        photos = self._get_scoped_photos()
        if photos is None:
            return

        assert self._library_root is not None and self._db_path is not None
        self._set_running(True)
        self._status_label.setText(f"Matching {len(candidate_tags)} tag(s) across {len(photos)} photos ...")

        worker = _MatchTagsWorker(
            photos=photos,
            candidate_tags=candidate_tags,
            library_root=self._library_root,
            db_path=self._db_path,
            api_url=self._ai_api_url,
            model_name=self._ai_model_name,
            vision_service=self._vision_service,
            repository=self._repository,
        )
        self._connect_worker(worker)
        self._worker = worker
        worker.start()

    # ── Feature 2: run autotags ────────────────────────────────

    def _run_autotags(self) -> None:
        if not self._check_ai_ready():
            return
        photos = self._get_scoped_photos()
        if photos is None:
            return

        assert self._library_root is not None and self._db_path is not None
        self._set_running(True)
        self._status_label.setText(f"Recalculating autotags for {len(photos)} photos ...")

        worker = _AutotagsWorker(
            photos=photos,
            library_root=self._library_root,
            db_path=self._db_path,
            api_url=self._ai_api_url,
            model_name=self._ai_model_name,
            vision_service=self._vision_service,
            repository=self._repository,
        )
        self._connect_worker(worker)
        self._worker = worker
        worker.start()

    # ── Feature 3: browse reference + run person tag ───────────

    def _browse_reference(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select reference photo",
            str(self._library_root) if self._library_root else "",
            "Images (*.jpg *.jpeg *.png *.webp *.bmp *.tiff)",
        )
        if path:
            self._reference_photo_path = Path(path)
            display = path if len(path) <= 60 else f"...{path[-57:]}"
            self._f3_ref_label.setText(display)
            self._f3_ref_label.setToolTip(path)
            self._f3_ref_label.setStyleSheet("")

    def _run_person_tag(self) -> None:
        if not self._check_ai_ready():
            return
        if self._reference_photo_path is None or not self._reference_photo_path.exists():
            QMessageBox.warning(self, "No reference", "Please select a valid reference photo first.")
            return
        person_tag = self._f3_name_input.text().strip()
        if not person_tag:
            QMessageBox.warning(self, "No name", "Please enter a person name tag.")
            return

        photos = self._get_scoped_photos()
        if photos is None:
            return

        assert self._library_root is not None and self._db_path is not None
        self._set_running(True)
        self._status_label.setText(
            f"Matching person '{person_tag}' across {len(photos)} photos ..."
        )

        worker = _PersonTagWorker(
            photos=photos,
            reference_path=self._reference_photo_path,
            person_tag=person_tag,
            library_root=self._library_root,
            db_path=self._db_path,
            api_url=self._ai_api_url,
            model_name=self._ai_model_name,
            vision_service=self._vision_service,
            repository=self._repository,
        )
        self._connect_worker(worker)
        self._worker = worker
        worker.start()
