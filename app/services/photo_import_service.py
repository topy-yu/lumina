from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from app.db.repository import PhotoRepository
from app.services.config_service import AppConfig, ConfigService
from app.services.file_service import FileService
from app.services.metadata_service import MetadataService


@dataclass(slots=True)
class FileImportResult:
    source: str
    status: str
    relative_path: str | None = None
    reason: str | None = None
    applied_tags: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ImportSummary:
    total: int = 0
    imported: int = 0
    duplicates: int = 0
    skipped_no_capture_time: int = 0
    errors: int = 0
    results: list[FileImportResult] = field(default_factory=list)


class PhotoImportService:
    def __init__(
        self,
        repository: PhotoRepository,
        metadata_service: MetadataService,
        file_service: FileService,
    ) -> None:
        self._repository = repository
        self._metadata_service = metadata_service
        self._file_service = file_service

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
    ) -> ImportSummary:
        summary = ImportSummary(total=len(files))
        lib_root = Path(config.library_root)
        db_path = lib_root / ConfigService.DB_FILENAME
        self._repository.initialize(db_path)
        folder_tag_rules = self._compile_folder_tag_rules(folder_tags_map or {})

        for source in files:
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
                self._repository.insert_photo(
                    db_path=db_path,
                    md5=md5,
                    relative_path=stored_relative,
                    capture_time_iso=capture_time.isoformat(),
                    tags_json=json.dumps(applied_tags, ensure_ascii=False),
                )
                summary.imported += 1
                summary.results.append(
                    FileImportResult(
                        source=str(source),
                        status="imported",
                        relative_path=stored_relative,
                        applied_tags=applied_tags,
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

