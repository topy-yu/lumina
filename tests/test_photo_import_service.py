from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from app.db.repository import PhotoRepository
from app.services.config_service import AppConfig, ConfigService
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
        AppConfig(library_root=str(lib)),
    )

    assert summary.total == 2
    assert summary.imported == 1
    assert summary.duplicates == 1
    assert summary.errors == 0
    assert summary.skipped_no_capture_time == 0


def test_import_skips_missing_capture_time(tmp_path: Path) -> None:
    src = tmp_path / "src"
    lib = tmp_path / "lib"
    lib.mkdir()

    file1 = src / "unknown.jpg"
    _write_file(file1, b"content")

    metadata = FakeMetadataService({"unknown.jpg": None})
    service = PhotoImportService(PhotoRepository(), metadata, FileService())
    summary = service.import_files(
        [file1],
        AppConfig(library_root=str(lib)),
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


def test_import_applies_folder_tags_recursively(tmp_path: Path) -> None:
    src = tmp_path / "src"
    root = src / "root"
    sub = root / "sub"
    other = src / "other"
    lib = tmp_path / "lib"
    lib.mkdir()

    file_a = root / "a.jpg"
    file_b = sub / "b.jpg"
    file_c = other / "c.jpg"
    _write_file(file_a, b"image-a")
    _write_file(file_b, b"image-b")
    _write_file(file_c, b"image-c")

    metadata = FakeMetadataService(
        {
            "a.jpg": datetime(2021, 1, 2, 10, 0, 0),
            "b.jpg": datetime(2021, 1, 2, 10, 1, 0),
            "c.jpg": datetime(2021, 1, 2, 10, 2, 0),
        }
    )
    service = PhotoImportService(PhotoRepository(), metadata, FileService())
    summary = service.import_files(
        [file_a, file_b, file_c],
        AppConfig(library_root=str(lib)),
        folder_tags_map={
            str(root): ["family", "trip"],
            str(src): ["all", "trip"],
        },
    )

    assert summary.imported == 3
    imported = [r for r in summary.results if r.status == "imported"]
    assert len(imported) == 3
    assert imported[0].applied_tags == ["family", "trip", "all"]
    assert imported[1].applied_tags == ["family", "trip", "all"]
    assert imported[2].applied_tags == ["all", "trip"]

    db_path = lib / ConfigService.DB_FILENAME
    repository = PhotoRepository()
    a_record = repository.get_photo(db_path, hashlib.md5(b"image-a").hexdigest())
    b_record = repository.get_photo(db_path, hashlib.md5(b"image-b").hexdigest())
    c_record = repository.get_photo(db_path, hashlib.md5(b"image-c").hexdigest())

    assert a_record is not None
    assert b_record is not None
    assert c_record is not None
    assert json.loads(a_record.tags) == ["family", "trip", "all"]
    assert json.loads(b_record.tags) == ["family", "trip", "all"]
    assert json.loads(c_record.tags) == ["all", "trip"]


def test_import_without_folder_tags_keeps_empty_tags(tmp_path: Path) -> None:
    src = tmp_path / "src"
    lib = tmp_path / "lib"
    lib.mkdir()

    file1 = src / "plain.jpg"
    _write_file(file1, b"plain-content")

    metadata = FakeMetadataService({"plain.jpg": datetime(2022, 2, 3, 4, 5, 6)})
    service = PhotoImportService(PhotoRepository(), metadata, FileService())
    summary = service.import_files([file1], AppConfig(library_root=str(lib)))

    assert summary.imported == 1
    assert summary.results[0].applied_tags == []

    db_path = lib / ConfigService.DB_FILENAME
    record = PhotoRepository().get_photo(db_path, hashlib.md5(b"plain-content").hexdigest())
    assert record is not None
    assert json.loads(record.tags) == []
