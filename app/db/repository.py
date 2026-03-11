from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class PhotoRecord:
    md5: str
    relative_path: str
    capture_time: str | None
    tags: str
    autotags: str


class PhotoRepository:
    def initialize(self, db_path: Path) -> None:
        schema = (Path(__file__).resolve().parent / "schema.sql").read_text(encoding="utf-8")
        with sqlite3.connect(db_path) as conn:
            conn.executescript(schema)
            self._ensure_columns(conn)
            conn.commit()

    @staticmethod
    def _ensure_columns(conn: sqlite3.Connection) -> None:
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(photos)").fetchall()
        }
        if "autotags" not in columns:
            conn.execute("ALTER TABLE photos ADD COLUMN autotags TEXT NOT NULL DEFAULT '[]'")

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
        tags_json: str = "[]",
        autotags_json: str = "[]",
    ) -> None:
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                INSERT INTO photos (md5, relative_path, capture_time, tags, autotags)
                VALUES (?, ?, ?, ?, ?)
                """,
                (md5, relative_path, capture_time_iso, tags_json, autotags_json),
            )
            conn.commit()

    def get_photo(self, db_path: Path, md5: str) -> PhotoRecord | None:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                """
                SELECT md5, relative_path, capture_time, tags, autotags
                FROM photos
                WHERE md5 = ?
                LIMIT 1
                """,
                (md5,),
            ).fetchone()
            if row is None:
                return None
            return PhotoRecord(*row)

    def update_relative_path(self, db_path: Path, old_relative_path: str, new_relative_path: str) -> None:
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                UPDATE photos
                SET relative_path = ?
                WHERE relative_path = ?
                """,
                (new_relative_path, old_relative_path),
            )
            conn.commit()

    def delete_by_relative_path(self, db_path: Path, relative_path: str) -> None:
        with sqlite3.connect(db_path) as conn:
            conn.execute("DELETE FROM photos WHERE relative_path = ?", (relative_path,))
            conn.commit()

    def get_all_photos(self, db_path: Path) -> list[PhotoRecord]:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT md5, relative_path, capture_time, tags, autotags FROM photos"
            ).fetchall()
            return [PhotoRecord(*row) for row in rows]

    def delete_by_md5(self, db_path: Path, md5: str) -> None:
        with sqlite3.connect(db_path) as conn:
            conn.execute("DELETE FROM photos WHERE md5 = ?", (md5,))
            conn.commit()

    def update_path_by_md5(self, db_path: Path, md5: str, new_relative_path: str) -> None:
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE photos SET relative_path = ? WHERE md5 = ?",
                (new_relative_path, md5),
            )
            conn.commit()

    def update_tags_by_md5(self, db_path: Path, md5: str, tags_json: str) -> None:
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE photos SET tags = ? WHERE md5 = ?",
                (tags_json, md5),
            )
            conn.commit()

    def update_autotags_by_md5(self, db_path: Path, md5: str, autotags_json: str) -> None:
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE photos SET autotags = ? WHERE md5 = ?",
                (autotags_json, md5),
            )
            conn.commit()
