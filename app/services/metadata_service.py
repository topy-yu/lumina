from __future__ import annotations

import logging
import platform
import re
from datetime import datetime
from pathlib import Path

from PIL import Image, UnidentifiedImageError
from PIL.ExifTags import IFD, TAGS

logger = logging.getLogger("lumina.metadata")

_HAS_PROPSYS = False
if platform.system() == "Windows":
    try:
        from win32com.propsys import propsys, pscon  # type: ignore[import-untyped]

        _HAS_PROPSYS = True
    except ImportError:
        pass


class MetadataService:
    _filename_patterns = [
        re.compile(r"IMG[_-]?(?P<d>\d{8})[_-]?(?P<t>\d{4,6})", flags=re.IGNORECASE),
        re.compile(r"(?<!\d)(?P<ts>\d{14})(?!\d)"),
        re.compile(r"(?<!\d)(?P<d>\d{8})[_-]?(?P<t>\d{6})(?!\d)"),
    ]

    def resolve_capture_time(self, path: Path) -> datetime | None:
        from_exif = self.extract_capture_time_from_exif(path)
        if from_exif is not None:
            logger.debug("Capture time from EXIF: %s -> %s", path.name, from_exif)
            return from_exif
        from_shell = self.extract_capture_time_from_shell(path)
        if from_shell is not None:
            logger.debug("Capture time from Windows shell: %s -> %s", path.name, from_shell)
            return from_shell
        from_filename = self.guess_capture_time_from_filename(path.name)
        if from_filename is not None:
            logger.debug("Capture time from filename: %s -> %s", path.name, from_filename)
        else:
            logger.debug("No capture time found for: %s", path.name)
        return from_filename

    def extract_capture_time_from_exif(self, path: Path) -> datetime | None:
        try:
            with Image.open(path) as img:
                exif = img.getexif()
        except (UnidentifiedImageError, OSError):
            return None

        exif_ifd = exif.get_ifd(IFD.Exif)
        for tag_id in (0x9003, 0x9004):
            parsed = self._parse_exif_datetime(exif_ifd.get(tag_id))
            if parsed is not None:
                return parsed

        main_lookup = {TAGS.get(k, k): v for k, v in exif.items()}
        return self._parse_exif_datetime(main_lookup.get("DateTime"))

    @staticmethod
    def extract_capture_time_from_shell(path: Path) -> datetime | None:
        """Read System.Photo.DateTaken via Windows shell property store (pywin32)."""
        if not _HAS_PROPSYS:
            return None
        try:
            store = propsys.SHGetPropertyStoreFromParsingName(str(path))
            val = store.GetValue(pscon.PKEY_Photo_DateTaken).GetValue()
            if val is None:
                return None
            if isinstance(val, datetime):
                return val
            return datetime.fromisoformat(str(val))
        except Exception:  # noqa: BLE001
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
