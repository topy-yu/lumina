from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from app.db.repository import PhotoRecord, PhotoRepository
from app.services.config_service import AppConfig
from app.services.file_service import FileService
from app.services.metadata_service import MetadataService


@dataclass(slots=True)
class CheckFileResult:
    status: str
    relative_path: str
    old_path: str | None = None
    new_path: str | None = None
    reason: str | None = None


@dataclass(slots=True)
class DbCheckSummary:
    total_on_disk: int = 0
    total_in_db: int = 0
    added: int = 0
    moved: int = 0
    deleted: int = 0
    errors: int = 0
    results: list[CheckFileResult] = field(default_factory=list)


class DbCheckService:
    def __init__(
        self,
        repository: PhotoRepository,
        metadata_service: MetadataService,
        file_service: FileService,
    ) -> None:
        self._repository = repository
        self._metadata_service = metadata_service
        self._file_service = file_service

    def check(
        self,
        config: AppConfig,
        progress: Callable[[str], None] | None = None,
    ) -> DbCheckSummary:
        summary = DbCheckSummary()
        lib_root = Path(config.library_root)
        db_path = Path(config.db_path)

        def report(msg: str) -> None:
            if progress:
                progress(msg)

        self._repository.initialize(db_path)

        report("Scanning library...")
        disk_files: dict[str, Path] = {}
        for path in lib_root.rglob("*"):
            if self._file_service.is_supported_photo(path):
                rel = os.path.normpath(str(path.relative_to(lib_root)))
                disk_files[rel] = path
        summary.total_on_disk = len(disk_files)
        report(f"Found {len(disk_files)} photo(s) on disk.")

        report("Loading database...")
        all_records = self._repository.get_all_photos(db_path)
        db_by_path: dict[str, PhotoRecord] = {
            os.path.normpath(r.relative_path): r for r in all_records
        }
        db_by_md5: dict[str, PhotoRecord] = {r.md5: r for r in all_records}
        summary.total_in_db = len(all_records)
        report(f"Found {len(all_records)} record(s) in database.")

        new_on_disk = {rel: p for rel, p in disk_files.items() if rel not in db_by_path}
        missing = {rel: rec for rel, rec in db_by_path.items() if rel not in disk_files}

        if not new_on_disk and not missing:
            report("Database is consistent. No changes needed.")
            return summary

        report(f"Untracked files: {len(new_on_disk)}, Missing from disk: {len(missing)}")

        report("Computing checksums for untracked files...")
        new_md5s: dict[str, str] = {}
        for i, (rel, abs_path) in enumerate(new_on_disk.items(), 1):
            if i % 20 == 0:
                report(f"  checksum {i}/{len(new_on_disk)}...")
            try:
                new_md5s[rel] = self._file_service.compute_md5(abs_path)
            except Exception as exc:  # noqa: BLE001
                summary.errors += 1
                summary.results.append(
                    CheckFileResult(status="error", relative_path=rel, reason=f"MD5 failed: {exc}")
                )

        moved_md5s: set[str] = set()

        report("Processing untracked files...")
        for rel, abs_path in new_on_disk.items():
            md5 = new_md5s.get(rel)
            if md5 is None:
                continue
            if md5 in db_by_md5:
                moved_md5s.add(md5)
                try:
                    self._handle_moved_file(
                        abs_path, rel, md5, db_by_md5[md5], lib_root, db_path, summary,
                    )
                except Exception as exc:  # noqa: BLE001
                    summary.errors += 1
                    summary.results.append(
                        CheckFileResult(status="error", relative_path=rel, reason=f"Move failed: {exc}")
                    )
            else:
                try:
                    self._handle_new_file(abs_path, rel, md5, db_path, summary)
                except Exception as exc:  # noqa: BLE001
                    summary.errors += 1
                    summary.results.append(
                        CheckFileResult(status="error", relative_path=rel, reason=f"Add failed: {exc}")
                    )

        report("Checking for deleted files...")
        for rel, record in missing.items():
            if record.md5 in moved_md5s:
                continue
            try:
                self._repository.delete_by_md5(db_path, record.md5)
                summary.deleted += 1
                summary.results.append(
                    CheckFileResult(status="deleted", relative_path=rel)
                )
            except Exception as exc:  # noqa: BLE001
                summary.errors += 1
                summary.results.append(
                    CheckFileResult(status="error", relative_path=rel, reason=f"Delete failed: {exc}")
                )

        report("Database check complete.")
        return summary

    def _handle_new_file(
        self,
        abs_path: Path,
        rel: str,
        md5: str,
        db_path: Path,
        summary: DbCheckSummary,
    ) -> None:
        capture_time = self._metadata_service.resolve_capture_time(abs_path)
        self._repository.insert_photo(
            db_path=db_path,
            md5=md5,
            relative_path=rel,
            capture_time_iso=capture_time.isoformat() if capture_time else None,
        )
        summary.added += 1
        summary.results.append(
            CheckFileResult(
                status="added",
                relative_path=rel,
                reason="capture time from metadata" if capture_time else "no capture time",
            )
        )

    def _handle_moved_file(
        self,
        abs_path: Path,
        rel: str,
        md5: str,
        old_record: PhotoRecord,
        lib_root: Path,
        db_path: Path,
        summary: DbCheckSummary,
    ) -> None:
        capture_time = self._metadata_service.resolve_capture_time(abs_path)
        if capture_time is None:
            self._repository.update_path_by_md5(db_path, md5, rel)
            summary.moved += 1
            summary.results.append(
                CheckFileResult(
                    status="moved",
                    relative_path=rel,
                    old_path=old_record.relative_path,
                    new_path=rel,
                    reason="no capture time, kept current location",
                )
            )
            return

        year = capture_time.strftime("%Y")
        month = capture_time.strftime("%m")
        target = lib_root / year / month / abs_path.name
        target = self._reserve_unique(target, abs_path)

        if target.resolve() != abs_path.resolve():
            self._file_service.move_file(abs_path, target)

        new_rel = str(target.relative_to(lib_root))
        self._repository.update_path_by_md5(db_path, md5, new_rel)
        summary.moved += 1
        summary.results.append(
            CheckFileResult(
                status="moved",
                relative_path=new_rel,
                old_path=old_record.relative_path,
                new_path=new_rel,
            )
        )

    @staticmethod
    def _reserve_unique(target: Path, source: Path) -> Path:
        if not target.exists():
            return target
        if target.resolve() == source.resolve():
            return target
        candidate = target
        while candidate.exists():
            candidate = candidate.parent / f"{candidate.stem}_DUP{candidate.suffix}"
        return candidate
