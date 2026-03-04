from __future__ import annotations

import hashlib
import random
import shutil
from datetime import datetime
from pathlib import Path


class FileService:
    _supported_extensions = {
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".heic",
        ".tif",
        ".tiff",
        ".bmp",
    }

    def is_supported_photo(self, path: Path) -> bool:
        return path.is_file() and path.suffix.lower() in self._supported_extensions

    def compute_md5(self, path: Path) -> str:
        digest = hashlib.md5()
        with path.open("rb") as file_handle:
            while True:
                chunk = file_handle.read(8192)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()

    def build_target_relative_path(self, capture_time: datetime, source_suffix: str) -> Path:
        year = capture_time.strftime("%Y")
        month = capture_time.strftime("%m")
        timestamp = capture_time.strftime("%Y%m%d%H%M")
        random_suffix = f"{random.randint(0, 99_999_999):08d}"
        extension = source_suffix.upper() if source_suffix else ".JPG"
        filename = f"IMG{timestamp}_{random_suffix}{extension}"
        return Path(year) / month / filename

    def ensure_parent(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)

    def move_file(self, source: Path, target: Path) -> None:
        self.ensure_parent(target)
        shutil.move(str(source), str(target))

