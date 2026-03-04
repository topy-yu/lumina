from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from PIL import Image, UnidentifiedImageError
from PIL.ExifTags import TAGS


class MetadataService:
    _filename_patterns = [
        re.compile(r"(?P<ts>\d{14})"),
        re.compile(r"(?P<d>\d{8})[_-]?(?P<t>\d{6})"),
        re.compile(r"IMG[_-]?(?P<d>\d{8})[_-]?(?P<t>\d{4,6})", flags=re.IGNORECASE),
    ]

    def resolve_capture_time(self, path: Path) -> datetime | None:
        from_exif = self.extract_capture_time_from_exif(path)
        if from_exif is not None:
            return from_exif
        return self.guess_capture_time_from_filename(path.name)

    def extract_capture_time_from_exif(self, path: Path) -> datetime | None:
        try:
            with Image.open(path) as img:
                exif = img.getexif()
                if not exif:
                    return None
        except (UnidentifiedImageError, OSError):
            return None

        exif_lookup = {TAGS.get(k, k): v for k, v in exif.items()}
        for key in ("DateTimeOriginal", "DateTimeDigitized", "DateTime"):
            raw = exif_lookup.get(key)
            parsed = self._parse_exif_datetime(raw)
            if parsed is not None:
                return parsed
        return None

    def guess_capture_time_from_filename(self, filename: str) -> datetime | None:
        stem = Path(filename).stem
        for pattern in self._filename_patterns:
            match = pattern.search(stem)
            if not match:
                continue
            if "ts" in match.groupdict():
                return self._parse_compact_ts(match.group("ts"))
            if "d" in match.groupdict() and "t" in match.groupdict():
                d = match.group("d")
                t = match.group("t")
                if len(t) == 4:
                    t = f"{t}00"
                ts = f"{d}{t}"
                return self._parse_compact_ts(ts)
        return None

    @staticmethod
    def _parse_exif_datetime(raw: object) -> datetime | None:
        if raw is None:
            return None
        try:
            text = str(raw).strip()
            return datetime.strptime(text, "%Y:%m:%d %H:%M:%S")
        except ValueError:
            return None

    @staticmethod
    def _parse_compact_ts(raw: str) -> datetime | None:
        try:
            return datetime.strptime(raw, "%Y%m%d%H%M%S")
        except ValueError:
            return None
