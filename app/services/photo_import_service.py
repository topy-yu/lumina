from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from app.db.repository import PhotoRepository
from app.services.config_service import AppConfig
from app.services.file_service import FileService
from app.services.metadata_service import MetadataService


@dataclass(slots=True)
class FileImportResult:
    source: str
    status: str
    relative_path: str | None = None
    reason: str | None = None


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

    def import_files(self, files: list[Path], config: AppConfig) -> ImportSummary:
        summary = ImportSummary(total=len(files))
        lib_root = Path(config.library_root)
        db_path = Path(config.db_path)
        self._repository.initialize(db_path)

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
                self._repository.insert_photo(
                    db_path=db_path,
                    md5=md5,
                    relative_path=stored_relative,
                    capture_time_iso=capture_time.isoformat(),
                )
                summary.imported += 1
                summary.results.append(
                    FileImportResult(
                        source=str(source),
                        status="imported",
                        relative_path=stored_relative,
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

