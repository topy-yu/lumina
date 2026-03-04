from __future__ import annotations

from datetime import datetime
from pathlib import Path

from app.db.repository import PhotoRepository
from app.services.config_service import AppConfig
from app.services.file_service import FileService
from app.services.photo_import_service import PhotoImportService


class FakeMetadataService:
    def __init__(self, mapping: dict[str, datetime | None]) -> None:
        self._mapping = mapping

    def resolve_capture_time(self, path: Path) -> datetime | None:
        return self._mapping.get(path.name)


def _write_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def test_import_skips_duplicate_by_md5(tmp_path: Path) -> None:
    src = tmp_path / "src"
    lib = tmp_path / "lib"
    db_file = tmp_path / "lumina.db"
    lib.mkdir()

    file1 = src / "a.jpg"
    file2 = src / "b.jpg"
    _write_file(file1, b"same-content")
    _write_file(file2, b"same-content")

    metadata = FakeMetadataService(
        {
            "a.jpg": datetime(2020, 1, 2, 3, 4, 5),
            "b.jpg": datetime(2020, 1, 2, 3, 4, 5),
        }
    )
    service = PhotoImportService(PhotoRepository(), metadata, FileService())
    summary = service.import_files(
        [file1, file2],
        AppConfig(library_root=str(lib), db_path=str(db_file)),
    )

    assert summary.total == 2
    assert summary.imported == 1
    assert summary.duplicates == 1
    assert summary.errors == 0
    assert summary.skipped_no_capture_time == 0


def test_import_skips_missing_capture_time(tmp_path: Path) -> None:
    src = tmp_path / "src"
    lib = tmp_path / "lib"
    db_file = tmp_path / "lumina.db"
    lib.mkdir()

    file1 = src / "unknown.jpg"
    _write_file(file1, b"content")

    metadata = FakeMetadataService({"unknown.jpg": None})
    service = PhotoImportService(PhotoRepository(), metadata, FileService())
    summary = service.import_files(
        [file1],
        AppConfig(library_root=str(lib), db_path=str(db_file)),
    )

    assert summary.total == 1
    assert summary.imported == 0
    assert summary.duplicates == 0
    assert summary.skipped_no_capture_time == 1
    assert summary.errors == 0


def test_build_target_relative_path_format() -> None:
    fs = FileService()
    rel = fs.build_target_relative_path(datetime(2000, 1, 2, 12, 34, 56), ".jpg")
    parts = rel.parts
    assert parts[0] == "2000"
    assert parts[1] == "01"
    assert rel.name.startswith("IMG200001021234_")
    assert rel.suffix == ".JPG"
