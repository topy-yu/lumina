from __future__ import annotations

import base64
import io
import json
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image

_TAG_PROMPT = (
    "Analyze this photo and provide relevant tags for organizing a photo library. "
    "Return ONLY a comma-separated list of short tags in English. Include relevant "
    "categories such as scene type, setting, objects, people, activities, time of day, "
    "season, colors. "
    "Example: landscape, mountain, sunset, hiking, summer, orange sky\n"
    "Return ONLY the comma-separated tags, nothing else."
)

_MAX_IMAGE_SIZE = 1024
_TIMEOUT = 120


class VisionServiceError(RuntimeError):
    pass


class VisionService:
    def generate_autotags(
        self,
        image_path: Path,
        api_url: str,
        model_name: str,
        *,
        strict: bool = False,
    ) -> list[str]:
        if not api_url or not model_name:
            return []
        try:
            image_b64 = self._encode_image(image_path)
            raw_text = self._call_model(api_url, model_name, image_b64)
            return self._parse_tags(raw_text)
        except Exception as exc:  # noqa: BLE001
            if strict:
                raise VisionServiceError(str(exc)) from exc
            return []

    @staticmethod
    def _encode_image(image_path: Path) -> str:
        with Image.open(image_path) as img:
            img.thumbnail((_MAX_IMAGE_SIZE, _MAX_IMAGE_SIZE))
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85)
            return base64.b64encode(buf.getvalue()).decode("ascii")

    @staticmethod
    def _call_model(api_url: str, model_name: str, image_b64: str) -> str:
        url = api_url.rstrip("/")
        payload = json.dumps({
            "model": model_name,
            "prompt": _TAG_PROMPT,
            "images": [image_b64],
            "stream": False,
        }).encode("utf-8")
        req = urllib.request.Request(f"{url}/api/generate", data=payload, method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("response", "")

    @staticmethod
    def _parse_tags(text: str) -> list[str]:
        tags: list[str] = []
        seen: set[str] = set()
        for raw in text.split(","):
            clean = raw.strip().strip(".-").lower()
            if not clean or len(clean) > 50 or clean in seen:
                continue
            seen.add(clean)
            tags.append(clean)
        return tags
