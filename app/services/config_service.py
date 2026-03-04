from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(slots=True)
class AppConfig:
    library_root: str = ""
    db_path: str = ""


class ConfigService:
    def __init__(self, config_file: Path | None = None) -> None:
        default = Path.home() / ".lumina" / "config.json"
        self._config_file = config_file or default

    def load(self) -> AppConfig:
        if not self._config_file.exists():
            return AppConfig()
        data = json.loads(self._config_file.read_text(encoding="utf-8"))
        return AppConfig(
            library_root=data.get("library_root", ""),
            db_path=data.get("db_path", ""),
        )

    def save(self, config: AppConfig) -> None:
        self._config_file.parent.mkdir(parents=True, exist_ok=True)
        self._config_file.write_text(json.dumps(asdict(config), indent=2), encoding="utf-8")

    def validate(self, config: AppConfig) -> list[str]:
        errors: list[str] = []
        if not config.library_root:
            errors.append("Photo library folder is required.")
        if not config.db_path:
            errors.append("Database file is required.")
        if errors:
            return errors

        lib_path = Path(config.library_root)
        db_path = Path(config.db_path)

        if not lib_path.exists():
            errors.append("Photo library folder does not exist.")
        elif not lib_path.is_dir():
            errors.append("Photo library path must be a directory.")
        elif not self._is_writable_directory(lib_path):
            errors.append("Photo library folder is not writable.")

        if db_path.exists() and db_path.is_dir():
            errors.append("Database path points to a directory, not a file.")

        db_parent = db_path.parent
        if not db_parent.exists():
            try:
                db_parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                errors.append("Database parent folder cannot be created.")
                return errors
        if not self._is_writable_directory(db_parent):
            errors.append("Database parent folder is not writable.")

        return errors

    @staticmethod
    def _is_writable_directory(path: Path) -> bool:
        if not path.exists() or not path.is_dir():
            return False
        probe = path / f".lumina_write_probe_{uuid.uuid4().hex}"
        try:
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return True
        except OSError:
            return False
