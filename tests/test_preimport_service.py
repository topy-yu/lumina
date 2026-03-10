from __future__ import annotations

from datetime import datetime
from pathlib import Path

from app.db.repository import PhotoRepository
from app.services.config_service import AppConfig, ConfigService
from app.services.file_service import FileService
from app.services.metadata_service import MetadataService
from app.services.preimport_service import PreImportService


def _write_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def test_preimport_then_abort_import_when_source_changed(tmp_path: Path) -> None:
    src = tmp_path / "src"
    lib = tmp_path / "lib"
    lib.mkdir()
    pre_db = tmp_path / "preimport.db"

    file1 = src / "a.jpg"
    _write_file(file1, b"first-content")

    service = PreImportService(
        repository=PhotoRepository(),
        metadata_service=MetadataService(),
        file_service=FileService(),
        vision_service=None,
        db_path=pre_db,
    )
    config = AppConfig(library_root=str(lib))
    job_id = service.create_job(
        [file1],
        config,
        folder_tags_map={},
        folder_capture_time_map={str(src): "2024-01-01 12:00:00"},
    )
    state = service.run_preimport(job_id, config)
    assert state.prepared == 1

    _write_file(file1, b"changed-content")
    summary = service.import_prepared(job_id, config)
    assert summary.aborted
    assert "source changed" in summary.abort_reason


def test_preimport_import_cleans_items_and_merges_db(tmp_path: Path) -> None:
    src = tmp_path / "src"
    lib = tmp_path / "lib"
    lib.mkdir()
    pre_db = tmp_path / "preimport.db"

    file1 = src / "a.jpg"
    file2 = src / "b.jpg"
    _write_file(file1, b"a-content")
    _write_file(file2, b"b-content")

    service = PreImportService(
        repository=PhotoRepository(),
        metadata_service=MetadataService(),
        file_service=FileService(),
        vision_service=None,
        db_path=pre_db,
    )
    config = AppConfig(library_root=str(lib))
    job_id = service.create_job(
        [file1, file2],
        config,
        folder_tags_map={},
        folder_capture_time_map={str(src): datetime(2023, 2, 3, 4, 5, 6).isoformat(sep=" ")},
    )
    pre_state = service.run_preimport(job_id, config)
    assert pre_state.prepared == 2

    summary = service.import_prepared(job_id, config)
    assert not summary.aborted
    assert summary.imported == 2

    state_after = service.get_job_state(job_id)
    assert state_after.planned == 0
    assert state_after.prepared == 0
    assert state_after.failed == 0

    db_path = lib / ConfigService.DB_FILENAME
    rows = PhotoRepository().get_all_photos(db_path)
    assert len(rows) == 2


def test_retry_failed_with_user_capture_time(tmp_path: Path) -> None:
    """A file fails pre-import due to missing capture time.
    User edits capture time in the DB, then retry succeeds."""
    src = tmp_path / "src"
    lib = tmp_path / "lib"
    lib.mkdir()
    pre_db = tmp_path / "preimport.db"

    file1 = src / "no_exif.jpg"
    _write_file(file1, b"photo-without-exif")

    service = PreImportService(
        repository=PhotoRepository(),
        metadata_service=MetadataService(),
        file_service=FileService(),
        vision_service=None,
        db_path=pre_db,
    )
    config = AppConfig(library_root=str(lib))
    job_id = service.create_job(
        [file1],
        config,
        folder_tags_map={},
        folder_capture_time_map={},
    )

    state = service.run_preimport(job_id, config)
    assert state.failed == 1
    assert state.prepared == 0

    items = service.list_job_items(job_id)
    assert len(items) == 1
    assert items[0].state == "failed"
    assert items[0].error_message == "capture time unavailable"

    service.update_item_capture_time(items[0].item_id, "2024-06-15 10:30:00")

    service.reset_failed_to_planned(job_id)

    state2 = service.run_preimport(job_id, config)
    assert state2.prepared == 1
    assert state2.failed == 0

    items2 = service.list_job_items(job_id)
    assert items2[0].state == "prepared"
    assert items2[0].capture_time_iso == "2024-06-15T10:30:00"


def test_update_item_tags_persisted(tmp_path: Path) -> None:
    """Tags edited by user are persisted and returned in list_job_items."""
    src = tmp_path / "src"
    lib = tmp_path / "lib"
    lib.mkdir()
    pre_db = tmp_path / "preimport.db"

    file1 = src / "a.jpg"
    _write_file(file1, b"content")

    service = PreImportService(
        repository=PhotoRepository(),
        metadata_service=MetadataService(),
        file_service=FileService(),
        vision_service=None,
        db_path=pre_db,
    )
    config = AppConfig(library_root=str(lib))
    job_id = service.create_job(
        [file1],
        config,
        folder_tags_map={},
        folder_capture_time_map={str(src): "2024-01-01 12:00:00"},
    )

    items = service.list_job_items(job_id)
    assert len(items) == 1
    service.update_item_tags(items[0].item_id, '["vacation", "beach"]')

    items2 = service.list_job_items(job_id)
    assert items2[0].manual_tags_json == '["vacation", "beach"]'


def test_prepare_single_item_directly_to_prepared(tmp_path: Path) -> None:
    """prepare_single_item processes a failed item directly to prepared
    without going through planned state."""
    src = tmp_path / "src"
    lib = tmp_path / "lib"
    lib.mkdir()
    pre_db = tmp_path / "preimport.db"

    file1 = src / "no_exif.jpg"
    _write_file(file1, b"photo-bytes")

    service = PreImportService(
        repository=PhotoRepository(),
        metadata_service=MetadataService(),
        file_service=FileService(),
        vision_service=None,
        db_path=pre_db,
    )
    config = AppConfig(library_root=str(lib))
    job_id = service.create_job(
        [file1],
        config,
        folder_tags_map={},
        folder_capture_time_map={},
    )

    state = service.run_preimport(job_id, config)
    assert state.failed == 1

    items = service.list_job_items(job_id)
    assert items[0].state == "failed"

    service.update_item_capture_time(items[0].item_id, "2025-03-01 08:00:00")

    result = service.prepare_single_item(job_id, items[0].item_id, config)
    assert result.state == "prepared"
    assert result.capture_time_iso == "2025-03-01T08:00:00"
    assert result.planned_relative_path is not None
