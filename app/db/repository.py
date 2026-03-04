from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(slots=True)
class PhotoRecord:
    md5: str
    relative_path: str
    capture_time: str | None
    tags: str
    created_at: str
    updated_at: str


class PhotoRepository:
    def initialize(self, db_path: Path) -> None:
        schema = (Path(__file__).resolve().parent / "schema.sql").read_text(encoding="utf-8")
        with sqlite3.connect(db_path) as conn:
            conn.executescript(schema)
            conn.commit()

    def exists_md5(self, db_path: Path, md5: str) -> bool:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute("SELECT 1 FROM photos WHERE md5 = ? LIMIT 1", (md5,)).fetchone()
            return row is not None

    def insert_photo(
        self,
        db_path: Path,
        md5: str,
        relative_path: str,
        capture_time_iso: str | None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                INSERT INTO photos (md5, relative_path, capture_time, tags, created_at, updated_at)
                VALUES (?, ?, ?, '[]', ?, ?)
                """,
                (md5, relative_path, capture_time_iso, now, now),
            )
            conn.commit()

    def get_photo(self, db_path: Path, md5: str) -> PhotoRecord | None:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                """
                SELECT md5, relative_path, capture_time, tags, created_at, updated_at
                FROM photos
                WHERE md5 = ?
                LIMIT 1
                """,
                (md5,),
            ).fetchone()
            if row is None:
                return None
            return PhotoRecord(*row)
