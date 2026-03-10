from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from app.services.ai_model_service import get_provider
from app.db.repository import PhotoRepository
from app.services.config_service import AppConfig, ConfigService
from app.services.file_service import FileService
from app.services.metadata_service import MetadataService
from app.services.vision_service import VisionService, VisionServiceError


@dataclass(slots=True)
class FileImportResult:
    source: str
    status: str
    relative_path: str | None = None
    reason: str | None = None
    applied_tags: list[str] = field(default_factory=list)
    autotags: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ImportSummary:
    total: int = 0
    imported: int = 0
    duplicates: int = 0
    skipped_no_capture_time: int = 0
    errors: int = 0
    model_checked: bool = False
    model_ready: bool = False
    model_message: str = ""
    aborted: bool = False
    abort_reason: str = ""
    results: list[FileImportResult] = field(default_factory=list)


class PhotoImportService:
    def __init__(
        self,
        repository: PhotoRepository,
        metadata_service: MetadataService,
        file_service: FileService,
        vision_service: VisionService | None = None,
    ) -> None:
        self._repository = repository
        self._metadata_service = metadata_service
        self._file_service = file_service
        self._vision_service = vision_service

    def collect_supported_files(self, folder: Path) -> list[Path]:
        files: list[Path] = []
        for path in folder.rglob("*"):
            if self._file_service.is_supported_photo(path):
                files.append(path)
        return files

    def import_files(
        self,
        files: list[Path],
        config: AppConfig,
        folder_tags_map: dict[str, list[str]] | None = None,
        folder_capture_time_map: dict[str, str] | None = None,
        progress: Callable[[str], None] | None = None,
        require_model_ready: bool = True,
    ) -> ImportSummary:
        summary = ImportSummary(total=len(files))
        lib_root = Path(config.library_root)
        db_path = lib_root / ConfigService.DB_FILENAME
        self._repository.initialize(db_path)
        folder_tag_rules = self._compile_folder_tag_rules(folder_tags_map or {})
        folder_capture_rules = self._compile_folder_capture_rules(folder_capture_time_map or {})
        model_ready = False

        def report(msg: str) -> None:
            if progress:
                progress(msg)

        if self._vision_service and config.ai_api_url and config.ai_model_name:
            summary.model_checked = True
            try:
                provider = get_provider(config.ai_provider)
                model_status = provider.check_model(config.ai_api_url, config.ai_model_name)
                summary.model_ready = model_status.connected and model_status.loaded
                summary.model_message = model_status.message
                model_ready = summary.model_ready
                report(f"AI model check: {model_status.message}")
            except Exception as exc:  # noqa: BLE001
                summary.model_ready = False
                summary.model_message = f"Model check failed: {exc}"
                model_ready = False
                report(summary.model_message)
            if require_model_ready and not model_ready:
                summary.aborted = True
                summary.abort_reason = (
                    "Import aborted: AI model is not running or not reachable."
                )
                report(summary.abort_reason)
                return summary

        for idx, source in enumerate(files, 1):
            report(f"Processing {idx}/{len(files)}: {source.name}")
            if not source.exists():
                summary.errors += 1
                summary.results.append(
                    FileImportResult(source=str(source), status="error", reason="source not found")
                )
                continue

            try:
                md5 = self._file_service.compute_md5(source)
                if self._repository.exists_md5(db_path, md5):
                    summary.duplicates += 1
                    summary.results.append(FileImportResult(source=str(source), status="duplicate"))
                    continue

                capture_time = self._metadata_service.resolve_capture_time(source)
                fallback_used = False
                if capture_time is None:
                    capture_time = self._resolve_capture_time_for_source(source, folder_capture_rules)
                    fallback_used = capture_time is not None
                    if capture_time is None:
                        summary.skipped_no_capture_time += 1
                        summary.results.append(
                            FileImportResult(
                                source=str(source),
                                status="skipped-no-time",
                                reason="capture time unavailable",
                            )
                        )
                        continue

                relative_path = self._file_service.build_target_relative_path(capture_time, source.suffix)
                target = self._reserve_unique_target(lib_root / relative_path)
                self._file_service.move_file(source, target)
                stored_relative = str(target.relative_to(lib_root))
                applied_tags = self._resolve_tags_for_source(source, folder_tag_rules)

                autotags: list[str] = []
                if (
                    self._vision_service
                    and config.ai_api_url
                    and config.ai_model_name
                    and model_ready
                ):
                    report(f"Auto-tagging {idx}/{len(files)}: {source.name}")
                    try:
                        autotags = self._vision_service.generate_autotags(
                            target, config.ai_api_url, config.ai_model_name, strict=True,
                        )
                    except VisionServiceError as exc:
                        if require_model_ready:
                            summary.errors += 1
                            summary.aborted = True
                            summary.abort_reason = f"Import aborted during model call: {exc}"
                            summary.results.append(
                                FileImportResult(
                                    source=str(source),
                                    status="error",
                                    relative_path=stored_relative,
                                    reason=summary.abort_reason,
                                    applied_tags=applied_tags,
                                )
                            )
                            report(summary.abort_reason)
                            return summary
                        report(f"Auto-tag skipped due to model error: {exc}")

                self._repository.insert_photo(
                    db_path=db_path,
                    md5=md5,
                    relative_path=stored_relative,
                    capture_time_iso=capture_time.isoformat(),
                    tags_json=json.dumps(applied_tags, ensure_ascii=False),
                    autotags_json=json.dumps(autotags, ensure_ascii=False),
                )
                summary.imported += 1
                summary.results.append(
                    FileImportResult(
                        source=str(source),
                        status="imported",
                        relative_path=stored_relative,
                        reason="capture time from folder rule" if fallback_used else None,
                        applied_tags=applied_tags,
                        autotags=autotags,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                summary.errors += 1
                summary.results.append(
                    FileImportResult(source=str(source), status="error", reason=str(exc))
                )
        return summary

    def _reserve_unique_target(self, target: Path) -> Path:
        candidate = target
        while candidate.exists():
            stem = candidate.stem
            suffix = candidate.suffix
            parent = candidate.parent
            candidate = parent / f"{stem}_DUP{suffix}"
        return candidate

    def update_relative_path_record(
        self,
        config: AppConfig,
        old_relative_path: str,
        new_relative_path: str,
    ) -> None:
        db_path = Path(config.db_path)
        self._repository.update_relative_path(db_path, old_relative_path, new_relative_path)

    def delete_relative_path_record(self, config: AppConfig, relative_path: str) -> None:
        db_path = Path(config.db_path)
        self._repository.delete_by_relative_path(db_path, relative_path)

    @staticmethod
    def _compile_folder_tag_rules(folder_tags_map: dict[str, list[str]]) -> list[tuple[Path, list[str]]]:
        rules: list[tuple[Path, list[str]]] = []
        for folder_str, tags in folder_tags_map.items():
            folder = Path(folder_str).resolve(strict=False)
            normalized_tags: list[str] = []
            seen: set[str] = set()
            for tag in tags:
                clean = tag.strip()
                if not clean or clean in seen:
                    continue
                seen.add(clean)
                normalized_tags.append(clean)
            if normalized_tags:
                rules.append((folder, normalized_tags))
        return rules

    @staticmethod
    def _resolve_tags_for_source(source: Path, rules: list[tuple[Path, list[str]]]) -> list[str]:
        resolved_source = source.resolve(strict=False)
        merged: list[str] = []
        seen: set[str] = set()
        for folder, tags in rules:
            if not PhotoImportService._is_path_under(resolved_source, folder):
                continue
            for tag in tags:
                if tag in seen:
                    continue
                seen.add(tag)
                merged.append(tag)
        return merged

    @staticmethod
    def _is_path_under(candidate: Path, folder: Path) -> bool:
        candidate_norm = os.path.normcase(str(candidate))
        folder_norm = os.path.normcase(str(folder))
        if candidate_norm == folder_norm:
            return True
        prefix = folder_norm.rstrip("\\/") + os.sep
        return candidate_norm.startswith(prefix)

    @staticmethod
    def _compile_folder_capture_rules(folder_capture_time_map: dict[str, str]) -> list[tuple[Path, datetime]]:
        rules: list[tuple[Path, datetime]] = []
        for folder_str, capture_value in folder_capture_time_map.items():
            try:
                capture_time = datetime.fromisoformat(capture_value.strip())
            except ValueError:
                continue
            folder = Path(folder_str).resolve(strict=False)
            rules.append((folder, capture_time))
        return rules

    @staticmethod
    def _resolve_capture_time_for_source(
        source: Path,
        rules: list[tuple[Path, datetime]],
    ) -> datetime | None:
        resolved_source = source.resolve(strict=False)
        best_match: tuple[int, datetime] | None = None
        for folder, capture_time in rules:
            if not PhotoImportService._is_path_under(resolved_source, folder):
                continue
            folder_len = len(os.path.normcase(str(folder)))
            if best_match is None or folder_len > best_match[0]:
                best_match = (folder_len, capture_time)
        if best_match is None:
            return None
        return best_match[1]

