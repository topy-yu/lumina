from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path

from app.db.repository import PhotoRepository
from app.services.config_service import AppConfig, ConfigService
from app.services.db_check_service import DbCheckService
from app.services.file_service import FileService


class FakeMetadataService:
    def __init__(self, mapping: dict[str, datetime | None]) -> None:
        self._mapping = mapping

    def resolve_capture_time(self, path: Path) -> datetime | None:
        return self._mapping.get(path.name)


def _write_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _setup_library(lib: Path) -> tuple[PhotoRepository, Path]:
    repo = PhotoRepository()
    db_path = lib / ConfigService.DB_FILENAME
    repo.initialize(db_path)
    return repo, db_path


def _results_by_status(summary, status: str):
    return [r for r in summary.results if r.status == status]


def test_new_file_is_added_to_db(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    repo, db_path = _setup_library(lib)

    _write_file(lib / "photo.jpg", b"new-photo")

    metadata = FakeMetadataService({"photo.jpg": datetime(2024, 3, 15, 10, 0, 0)})
    service = DbCheckService(repo, metadata, FileService())
    config = AppConfig(library_root=str(lib), db_path=str(db_path))
    summary = service.check(config)

    assert summary.added == 1
    assert summary.moved == 0
    assert summary.deleted == 0

    added = _results_by_status(summary, "added")
    assert len(added) == 1
    assert "photo.jpg" in added[0].relative_path

    md5 = hashlib.md5(b"new-photo").hexdigest()
    record = repo.get_photo(db_path, md5)
    assert record is not None
    assert record.capture_time == "2024-03-15T10:00:00"


def test_new_file_without_capture_time_still_added(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    repo, db_path = _setup_library(lib)

    _write_file(lib / "random.jpg", b"no-time")

    metadata = FakeMetadataService({"random.jpg": None})
    service = DbCheckService(repo, metadata, FileService())
    config = AppConfig(library_root=str(lib), db_path=str(db_path))
    summary = service.check(config)

    assert summary.added == 1
    added = _results_by_status(summary, "added")
    assert len(added) == 1
    assert added[0].reason == "no capture time"

    md5 = hashlib.md5(b"no-time").hexdigest()
    record = repo.get_photo(db_path, md5)
    assert record is not None
    assert record.capture_time is None


def test_moved_file_updates_db_and_reorganizes(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    repo, db_path = _setup_library(lib)

    content = b"moved-photo"
    md5 = hashlib.md5(content).hexdigest()
    repo.insert_photo(
        db_path, md5, "2024/03/old_name.jpg", "2024-03-15T10:00:00",
    )

    _write_file(lib / "inbox" / "IMG20240315_100000.jpg", content)

    metadata = FakeMetadataService(
        {"IMG20240315_100000.jpg": datetime(2024, 3, 15, 10, 0, 0)}
    )
    service = DbCheckService(repo, metadata, FileService())
    config = AppConfig(library_root=str(lib), db_path=str(db_path))
    summary = service.check(config)

    assert summary.moved == 1
    assert summary.deleted == 0

    moved = _results_by_status(summary, "moved")
    assert len(moved) == 1
    assert moved[0].old_path == "2024/03/old_name.jpg"
    assert moved[0].new_path is not None

    record = repo.get_photo(db_path, md5)
    assert record is not None
    assert record.relative_path.startswith("2024")
    assert record.relative_path.endswith(".jpg")

    new_file = lib / record.relative_path
    assert new_file.exists()
    assert not (lib / "inbox" / "IMG20240315_100000.jpg").exists()


def test_moved_file_without_capture_time_updates_path_only(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    repo, db_path = _setup_library(lib)

    content = b"moved-no-time"
    md5 = hashlib.md5(content).hexdigest()
    repo.insert_photo(db_path, md5, "old/path.jpg", None)

    new_rel = str(Path("somewhere") / "file.jpg")
    _write_file(lib / new_rel, content)

    metadata = FakeMetadataService({"file.jpg": None})
    service = DbCheckService(repo, metadata, FileService())
    config = AppConfig(library_root=str(lib), db_path=str(db_path))
    summary = service.check(config)

    assert summary.moved == 1
    moved = _results_by_status(summary, "moved")
    assert len(moved) == 1
    assert moved[0].reason == "no capture time, kept current location"

    record = repo.get_photo(db_path, md5)
    assert record is not None
    assert record.relative_path == new_rel


def test_truly_deleted_file_removed_from_db(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    repo, db_path = _setup_library(lib)

    md5 = hashlib.md5(b"deleted-content").hexdigest()
    repo.insert_photo(db_path, md5, "2024/01/gone.jpg", "2024-01-01T00:00:00")

    metadata = FakeMetadataService({})
    service = DbCheckService(repo, metadata, FileService())
    config = AppConfig(library_root=str(lib), db_path=str(db_path))
    summary = service.check(config)

    assert summary.deleted == 1
    deleted = _results_by_status(summary, "deleted")
    assert len(deleted) == 1
    assert "gone.jpg" in deleted[0].relative_path
    assert repo.get_photo(db_path, md5) is None


def test_moved_file_not_counted_as_deleted(tmp_path: Path) -> None:
    """A file whose old path is missing but whose MD5 appears in a new location
    should be treated as moved, not deleted."""
    lib = tmp_path / "lib"
    lib.mkdir()
    repo, db_path = _setup_library(lib)

    content = b"moved-not-deleted"
    md5 = hashlib.md5(content).hexdigest()
    repo.insert_photo(db_path, md5, "old/location.jpg", "2024-06-01T12:00:00")

    _write_file(lib / "new" / "location.jpg", content)

    metadata = FakeMetadataService({"location.jpg": datetime(2024, 6, 1, 12, 0, 0)})
    service = DbCheckService(repo, metadata, FileService())
    config = AppConfig(library_root=str(lib), db_path=str(db_path))
    summary = service.check(config)

    assert summary.moved == 1
    assert summary.deleted == 0
    assert repo.get_photo(db_path, md5) is not None


def test_consistent_db_reports_no_changes(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    repo, db_path = _setup_library(lib)

    content = b"tracked-file"
    md5 = hashlib.md5(content).hexdigest()
    rel = "2024/01/photo.jpg"
    _write_file(lib / rel, content)
    repo.insert_photo(db_path, md5, rel, "2024-01-01T00:00:00")

    metadata = FakeMetadataService({})
    service = DbCheckService(repo, metadata, FileService())
    config = AppConfig(library_root=str(lib), db_path=str(db_path))
    summary = service.check(config)

    assert summary.added == 0
    assert summary.moved == 0
    assert summary.deleted == 0
    assert summary.errors == 0
    assert len(summary.results) == 0


def test_summary_has_correct_totals(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    repo, db_path = _setup_library(lib)

    _write_file(lib / "a.jpg", b"file-a")
    _write_file(lib / "b.jpg", b"file-b")
    md5_a = hashlib.md5(b"file-a").hexdigest()
    repo.insert_photo(db_path, md5_a, "a.jpg", "2024-01-01T00:00:00")

    md5_gone = hashlib.md5(b"gone").hexdigest()
    repo.insert_photo(db_path, md5_gone, "gone.jpg", "2024-02-01T00:00:00")

    metadata = FakeMetadataService({"b.jpg": datetime(2024, 5, 1, 12, 0, 0)})
    service = DbCheckService(repo, metadata, FileService())
    config = AppConfig(library_root=str(lib), db_path=str(db_path))
    summary = service.check(config)

    assert summary.total_on_disk == 2
    assert summary.total_in_db == 2
    assert summary.added == 1
    assert summary.deleted == 1
    assert summary.moved == 0
    assert summary.errors == 0
    assert len(summary.results) == 2
