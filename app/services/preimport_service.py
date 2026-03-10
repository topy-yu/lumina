from __future__ import annotations

import json
import os
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from app.db.repository import PhotoRepository
from app.services.ai_model_service import get_provider
from app.services.config_service import AppConfig, ConfigService
from app.services.file_service import FileService
from app.services.metadata_service import MetadataService
from app.services.photo_import_service import FileImportResult, ImportSummary
from app.services.vision_service import VisionService, VisionServiceError


@dataclass(slots=True)
class PreImportJobState:
    job_id: str
    status: str
    planned: int
    prepared: int
    failed: int
    imported: int


@dataclass(slots=True)
class PreImportItemReport:
    item_id: int
    source_path: str
    state: str
    planned_relative_path: str | None
    capture_time_iso: str | None
    manual_tags_json: str
    autotags_json: str
    error_message: str | None


class PreImportService:
    def __init__(
        self,
        repository: PhotoRepository,
        metadata_service: MetadataService,
        file_service: FileService,
        vision_service: VisionService | None = None,
        db_path: Path | None = None,
    ) -> None:
        self._repository = repository
        self._metadata_service = metadata_service
        self._file_service = file_service
        self._vision_service = vision_service
        project_root = Path(__file__).resolve().parent.parent.parent
        self._db_path = db_path or (project_root / "preimport.db")

    def initialize(self) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    config_snapshot_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS job_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    source_mtime_ns INTEGER,
                    source_size INTEGER,
                    source_md5 TEXT,
                    capture_time_iso TEXT,
                    manual_tags_json TEXT NOT NULL DEFAULT '[]',
                    autotags_json TEXT NOT NULL DEFAULT '[]',
                    planned_relative_path TEXT,
                    state TEXT NOT NULL,
                    error_message TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_job_items_job_state ON job_items (job_id, state);
                CREATE INDEX IF NOT EXISTS idx_job_items_md5 ON job_items (source_md5);
                CREATE INDEX IF NOT EXISTS idx_job_items_source_path ON job_items (source_path);
                """
            )
            conn.commit()

    def latest_active_job_id(self) -> str | None:
        self.initialize()
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                """
                SELECT job_id FROM jobs
                WHERE status IN ('planned', 'running', 'interrupted', 'ready', 'failed', 'importing')
                ORDER BY created_at DESC
                LIMIT 1
                """
            ).fetchone()
            return row[0] if row else None

    def get_job_state(self, job_id: str) -> PreImportJobState:
        self.initialize()
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT status FROM jobs WHERE job_id = ? LIMIT 1",
                (job_id,),
            ).fetchone()
            status = row[0] if row else "missing"
            counts = {
                state: conn.execute(
                    "SELECT COUNT(1) FROM job_items WHERE job_id = ? AND state = ?",
                    (job_id, state),
                ).fetchone()[0]
                for state in ("planned", "prepared", "failed", "imported")
            }
        return PreImportJobState(
            job_id=job_id,
            status=status,
            planned=counts["planned"],
            prepared=counts["prepared"],
            failed=counts["failed"],
            imported=counts["imported"],
        )

    def list_job_items(self, job_id: str) -> list[PreImportItemReport]:
        self.initialize()
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, source_path, state, planned_relative_path,
                       capture_time_iso, manual_tags_json, autotags_json, error_message
                FROM job_items
                WHERE job_id = ?
                ORDER BY id
                """,
                (job_id,),
            ).fetchall()
        return [
            PreImportItemReport(
                item_id=int(row[0]),
                source_path=row[1],
                state=row[2],
                planned_relative_path=row[3],
                capture_time_iso=row[4],
                manual_tags_json=row[5] or "[]",
                autotags_json=row[6] or "[]",
                error_message=row[7],
            )
            for row in rows
        ]

    def update_item_tags(self, item_id: int, tags_json: str) -> None:
        self.initialize()
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "UPDATE job_items SET manual_tags_json = ?, updated_at = ? WHERE id = ?",
                (tags_json, datetime.now().isoformat(timespec="seconds"), item_id),
            )
            conn.commit()

    def update_item_capture_time(self, item_id: int, capture_time_iso: str) -> None:
        self.initialize()
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "UPDATE job_items SET capture_time_iso = ?, updated_at = ? WHERE id = ?",
                (capture_time_iso, datetime.now().isoformat(timespec="seconds"), item_id),
            )
            conn.commit()

    def reset_failed_to_planned(self, job_id: str) -> int:
        self.initialize()
        with sqlite3.connect(self._db_path) as conn:
            cursor = conn.execute(
                "UPDATE job_items SET state = 'planned', error_message = NULL, updated_at = ? WHERE job_id = ? AND state = 'failed'",
                (datetime.now().isoformat(timespec="seconds"), job_id),
            )
            conn.commit()
            return cursor.rowcount

    def reset_item_to_planned(self, item_id: int) -> None:
        self.initialize()
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "UPDATE job_items SET state = 'planned', error_message = NULL, updated_at = ? WHERE id = ?",
                (datetime.now().isoformat(timespec="seconds"), item_id),
            )
            conn.commit()

    def prepare_single_item(
        self,
        job_id: str,
        item_id: int,
        config: AppConfig,
    ) -> PreImportItemReport:
        """Process a single item directly to 'prepared' (or 'failed')."""
        self.initialize()
        rules = self._load_rules_for_job(job_id)

        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT id, source_path, manual_tags_json, capture_time_iso FROM job_items WHERE id = ? AND job_id = ?",
                (item_id, job_id),
            ).fetchone()
        if not row:
            raise RuntimeError(f"Item {item_id} not found in job {job_id}")

        source = Path(row[1])
        manual_tags = json.loads(row[2] or "[]")
        existing_capture_iso: str | None = row[3]

        if not source.exists() or not source.is_file():
            self._delete_item(item_id)
            raise RuntimeError(f"Source not found, removed from job: {source}")

        try:
            stat = source.stat()
            md5 = self._file_service.compute_md5(source)
            capture_time = self._metadata_service.resolve_capture_time(source)
            if capture_time is None:
                capture_time = self._resolve_capture_time_for_source(source, rules["capture_time"])
            if capture_time is None and existing_capture_iso:
                try:
                    capture_time = datetime.fromisoformat(existing_capture_iso)
                except ValueError:
                    pass
            if capture_time is None:
                raise RuntimeError("capture time unavailable")
            rel = self._file_service.build_target_relative_path(capture_time, source.suffix)
            autotags: list[str] = []
            if self._vision_service and config.ai_api_url and config.ai_model_name:
                autotags = self._vision_service.generate_autotags(
                    source, config.ai_api_url, config.ai_model_name, strict=True,
                )
            self._update_item_prepared(
                item_id=item_id,
                mtime_ns=stat.st_mtime_ns,
                size=stat.st_size,
                md5=md5,
                capture_time_iso=capture_time.isoformat(),
                manual_tags=manual_tags,
                autotags=autotags,
                planned_relative_path=str(rel),
            )
        except Exception as exc:  # noqa: BLE001
            self._update_item_failed(item_id, str(exc))

        items = self.list_job_items(job_id)
        for it in items:
            if it.item_id == item_id:
                return it
        raise RuntimeError(f"Source not found, removed from job: {source}")

    def create_job(
        self,
        files: list[Path],
        config: AppConfig,
        folder_tags_map: dict[str, list[str]],
        folder_capture_time_map: dict[str, str],
    ) -> str:
        self.initialize()
        job_id = uuid.uuid4().hex
        rules = self._compile_rules(folder_tags_map, folder_capture_time_map)
        snapshot = json.dumps(
            {
                "library_root": config.library_root,
                "ai_provider": config.ai_provider,
                "ai_api_url": config.ai_api_url,
                "ai_model_name": config.ai_model_name,
                "folder_tags_map": folder_tags_map,
                "folder_capture_time_map": folder_capture_time_map,
            },
            ensure_ascii=False,
        )
        now = datetime.now().isoformat(timespec="seconds")
        unique_sources: list[Path] = []
        seen: set[str] = set()
        for source in files:
            resolved = str(source.resolve(strict=False))
            if resolved in seen:
                continue
            seen.add(resolved)
            unique_sources.append(source)

        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "INSERT INTO jobs (job_id, created_at, status, config_snapshot_json) VALUES (?, ?, 'planned', ?)",
                (job_id, now, snapshot),
            )
            for source in unique_sources:
                tags = self._resolve_tags_for_source(source, rules["tags"])
                conn.execute(
                    """
                    INSERT INTO job_items (
                        job_id, source_path, manual_tags_json, state, updated_at
                    ) VALUES (?, ?, ?, 'planned', ?)
                    """,
                    (job_id, str(source), json.dumps(tags, ensure_ascii=False), now),
                )
            conn.commit()
        return job_id

    def run_preimport(
        self,
        job_id: str,
        config: AppConfig,
        *,
        progress: Callable[[str], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> PreImportJobState:
        self.initialize()
        self._set_job_status(job_id, "running")
        rules = self._load_rules_for_job(job_id)

        if self._vision_service and config.ai_api_url and config.ai_model_name:
            provider = get_provider(config.ai_provider)
            model_status = provider.check_model(config.ai_api_url, config.ai_model_name)
            if not (model_status.connected and model_status.loaded):
                self._set_job_status(job_id, "failed")
                raise RuntimeError(f"AI model not ready for pre-import: {model_status.message}")

        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, source_path, manual_tags_json, capture_time_iso
                FROM job_items
                WHERE job_id = ? AND state IN ('planned', 'failed')
                ORDER BY id
                """,
                (job_id,),
            ).fetchall()

        for idx, row in enumerate(rows, 1):
            if should_stop and should_stop():
                self._set_job_status(job_id, "interrupted")
                return self.get_job_state(job_id)

            item_id = int(row[0])
            source = Path(row[1])
            manual_tags = json.loads(row[2] or "[]")
            existing_capture_iso: str | None = row[3]
            if progress:
                progress(f"Pre-import {idx}/{len(rows)}: {source.name}")

            if not source.exists() or not source.is_file():
                if progress:
                    progress(f"Removed missing source: {source.name}")
                self._delete_item(item_id)
                continue

            try:
                stat = source.stat()
                md5 = self._file_service.compute_md5(source)
                capture_time = self._metadata_service.resolve_capture_time(source)
                if capture_time is None:
                    capture_time = self._resolve_capture_time_for_source(source, rules["capture_time"])
                if capture_time is None and existing_capture_iso:
                    try:
                        capture_time = datetime.fromisoformat(existing_capture_iso)
                    except ValueError:
                        pass
                if capture_time is None:
                    raise RuntimeError("capture time unavailable")
                rel = self._file_service.build_target_relative_path(capture_time, source.suffix)
                autotags: list[str] = []
                if self._vision_service and config.ai_api_url and config.ai_model_name:
                    autotags = self._vision_service.generate_autotags(
                        source,
                        config.ai_api_url,
                        config.ai_model_name,
                        strict=True,
                    )
                self._update_item_prepared(
                    item_id=item_id,
                    mtime_ns=stat.st_mtime_ns,
                    size=stat.st_size,
                    md5=md5,
                    capture_time_iso=capture_time.isoformat(),
                    manual_tags=manual_tags,
                    autotags=autotags,
                    planned_relative_path=str(rel),
                )
            except Exception as exc:  # noqa: BLE001
                self._update_item_failed(item_id, str(exc))

        state = self.get_job_state(job_id)
        self._set_job_status(job_id, "ready" if state.planned == 0 else "failed")
        return self.get_job_state(job_id)

    def import_prepared(
        self,
        job_id: str,
        config: AppConfig,
        *,
        progress: Callable[[str], None] | None = None,
    ) -> ImportSummary:
        self.initialize()
        summary = ImportSummary()
        lib_root = Path(config.library_root)
        db_path = lib_root / ConfigService.DB_FILENAME
        self._repository.initialize(db_path)

        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, source_path, source_mtime_ns, source_size, source_md5,
                       capture_time_iso, manual_tags_json, autotags_json, planned_relative_path
                FROM job_items
                WHERE job_id = ? AND state = 'prepared'
                ORDER BY id
                """,
                (job_id,),
            ).fetchall()

        summary.total = len(rows)
        self._set_job_status(job_id, "importing")
        validation_error = self._validate_prepared_rows(rows)
        if validation_error:
            summary.aborted = True
            summary.abort_reason = validation_error
            self._set_job_status(job_id, "failed")
            return summary

        for idx, row in enumerate(rows, 1):
            item_id = int(row[0])
            source = Path(row[1])
            md5 = row[4]
            capture_time_iso = row[5]
            manual_tags_json = row[6] or "[]"
            autotags_json = row[7] or "[]"
            planned_rel = row[8]
            if progress:
                progress(f"Import prepared {idx}/{len(rows)}: {source.name}")

            if self._repository.exists_md5(db_path, md5):
                summary.duplicates += 1
                summary.results.append(FileImportResult(source=str(source), status="duplicate"))
                self._delete_item(item_id)
                continue

            target = self._reserve_unique_target(lib_root / planned_rel)
            self._file_service.move_file(source, target)
            stored_relative = str(target.relative_to(lib_root))
            self._repository.insert_photo(
                db_path=db_path,
                md5=md5,
                relative_path=stored_relative,
                capture_time_iso=capture_time_iso,
                tags_json=manual_tags_json,
                autotags_json=autotags_json,
            )
            summary.imported += 1
            summary.results.append(
                FileImportResult(
                    source=str(source),
                    status="imported",
                    relative_path=stored_relative,
                    applied_tags=json.loads(manual_tags_json),
                    autotags=json.loads(autotags_json),
                )
            )
            self._delete_item(item_id)

        state = self.get_job_state(job_id)
        self._set_job_status(job_id, "done" if state.planned == 0 and state.prepared == 0 else "ready")
        return summary

    def _validate_prepared_rows(self, rows: list[tuple]) -> str | None:
        for row in rows:
            source = Path(row[1])
            mtime_ns = int(row[2])
            size = int(row[3])
            if not source.exists() or not source.is_file():
                return f"Import aborted: source missing: {source}"
            stat = source.stat()
            if stat.st_mtime_ns != mtime_ns or stat.st_size != size:
                return f"Import aborted: source changed after pre-import: {source}"
        return None

    def _set_job_status(self, job_id: str, status: str) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("UPDATE jobs SET status = ? WHERE job_id = ?", (status, job_id))
            conn.commit()

    def _delete_item(self, item_id: int) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("DELETE FROM job_items WHERE id = ?", (item_id,))
            conn.commit()

    def _update_item_prepared(
        self,
        *,
        item_id: int,
        mtime_ns: int,
        size: int,
        md5: str,
        capture_time_iso: str,
        manual_tags: list[str],
        autotags: list[str],
        planned_relative_path: str,
    ) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """
                UPDATE job_items
                SET source_mtime_ns = ?, source_size = ?, source_md5 = ?,
                    capture_time_iso = ?, manual_tags_json = ?, autotags_json = ?,
                    planned_relative_path = ?, state = 'prepared', error_message = NULL, updated_at = ?
                WHERE id = ?
                """,
                (
                    mtime_ns,
                    size,
                    md5,
                    capture_time_iso,
                    json.dumps(manual_tags, ensure_ascii=False),
                    json.dumps(autotags, ensure_ascii=False),
                    planned_relative_path,
                    datetime.now().isoformat(timespec="seconds"),
                    item_id,
                ),
            )
            conn.commit()

    def _update_item_failed(self, item_id: int, message: str) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """
                UPDATE job_items
                SET state = 'failed', error_message = ?, updated_at = ?
                WHERE id = ?
                """,
                (message, datetime.now().isoformat(timespec="seconds"), item_id),
            )
            conn.commit()

    @staticmethod
    def _is_path_under(candidate: Path, folder: Path) -> bool:
        candidate_norm = os.path.normcase(str(candidate.resolve(strict=False)))
        folder_norm = os.path.normcase(str(folder.resolve(strict=False)))
        if candidate_norm == folder_norm:
            return True
        return candidate_norm.startswith(folder_norm.rstrip("\\/") + os.sep)

    def _compile_rules(
        self,
        folder_tags_map: dict[str, list[str]],
        folder_capture_time_map: dict[str, str],
    ) -> dict[str, list[tuple]]:
        tag_rules: list[tuple[Path, list[str]]] = []
        for folder_str, tags in folder_tags_map.items():
            folder = Path(folder_str).resolve(strict=False)
            dedup: list[str] = []
            seen: set[str] = set()
            for tag in tags:
                clean = tag.strip()
                if clean and clean not in seen:
                    seen.add(clean)
                    dedup.append(clean)
            if dedup:
                tag_rules.append((folder, dedup))

        time_rules: list[tuple[Path, datetime]] = []
        for folder_str, capture_str in folder_capture_time_map.items():
            try:
                parsed = datetime.fromisoformat(capture_str.strip())
            except ValueError:
                continue
            time_rules.append((Path(folder_str).resolve(strict=False), parsed))
        return {"tags": tag_rules, "capture_time": time_rules}

    @staticmethod
    def _resolve_tags_for_source(source: Path, rules: list[tuple[Path, list[str]]]) -> list[str]:
        resolved = source.resolve(strict=False)
        merged: list[str] = []
        seen: set[str] = set()
        for folder, tags in rules:
            if not PreImportService._is_path_under(resolved, folder):
                continue
            for tag in tags:
                if tag not in seen:
                    seen.add(tag)
                    merged.append(tag)
        return merged

    @staticmethod
    def _resolve_capture_time_for_source(
        source: Path,
        rules: list[tuple[Path, datetime]],
    ) -> datetime | None:
        resolved = source.resolve(strict=False)
        best: tuple[int, datetime] | None = None
        for folder, capture in rules:
            if not PreImportService._is_path_under(resolved, folder):
                continue
            rank = len(str(folder))
            if best is None or rank > best[0]:
                best = (rank, capture)
        return best[1] if best else None

    def _load_rules_for_job(self, job_id: str) -> dict[str, list[tuple]]:
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT config_snapshot_json FROM jobs WHERE job_id = ? LIMIT 1",
                (job_id,),
            ).fetchone()
        if not row:
            return {"tags": [], "capture_time": []}
        try:
            snapshot = json.loads(row[0])
        except json.JSONDecodeError:
            return {"tags": [], "capture_time": []}
        return self._compile_rules(
            snapshot.get("folder_tags_map", {}),
            snapshot.get("folder_capture_time_map", {}),
        )

    @staticmethod
    def _reserve_unique_target(target: Path) -> Path:
        candidate = target
        while candidate.exists():
            candidate = candidate.with_name(f"{candidate.stem}_DUP{candidate.suffix}")
        return candidate
