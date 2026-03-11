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
    "Return ONLY a comma-separated list of short tags in both English and Chinese. "
    "For each tag, provide the English version followed by its Chinese translation in parentheses. "
    "Include relevant categories such as scene type, setting, objects, people, activities, "
    "time of day, season, colors. "
    "Example: landscape(风景), mountain(山), sunset(日落), hiking(徒步), summer(夏天), orange sky(橙色天空)\n"
    "Return ONLY the comma-separated tags, nothing else."
)

_MATCH_TAGS_PROMPT = (
    "Here are candidate tags: {candidates}\n"
    "Look at this photo and determine which of these candidate tags accurately describe "
    "the photo content. Return ONLY the matching tags from the list above as a "
    "comma-separated list. If none of the tags match, return exactly the word NONE.\n"
    "Return ONLY the comma-separated matching tags or NONE, nothing else."
)

_MATCH_PERSON_PROMPT = (
    "The first image shows a reference person. "
    "Look at the second image and determine whether the SAME person appears in it. "
    "Focus on facial features, hair, and overall appearance. "
    "Answer ONLY YES or NO, nothing else."
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
            raw_text = self._call_model(api_url, model_name, _TAG_PROMPT, [image_b64])
            return self._parse_tags(raw_text)
        except Exception as exc:  # noqa: BLE001
            if strict:
                raise VisionServiceError(str(exc)) from exc
            return []

    def match_tags(
        self,
        image_path: Path,
        api_url: str,
        model_name: str,
        candidate_tags: list[str],
        *,
        strict: bool = False,
    ) -> list[str]:
        """Return the subset of *candidate_tags* that the model considers matching."""
        if not api_url or not model_name or not candidate_tags:
            return []
        try:
            image_b64 = self._encode_image(image_path)
            prompt = _MATCH_TAGS_PROMPT.format(candidates=", ".join(candidate_tags))
            raw_text = self._call_model(api_url, model_name, prompt, [image_b64])
            return self._filter_candidates(raw_text, candidate_tags)
        except Exception as exc:  # noqa: BLE001
            if strict:
                raise VisionServiceError(str(exc)) from exc
            return []

    def match_person(
        self,
        reference_path: Path,
        target_path: Path,
        api_url: str,
        model_name: str,
        *,
        strict: bool = False,
    ) -> bool:
        """Return True if the person in *reference_path* appears in *target_path*."""
        if not api_url or not model_name:
            return False
        try:
            ref_b64 = self._encode_image(reference_path)
            tgt_b64 = self._encode_image(target_path)
            raw_text = self._call_model(
                api_url, model_name, _MATCH_PERSON_PROMPT, [ref_b64, tgt_b64],
            )
            return raw_text.strip().upper().startswith("YES")
        except Exception as exc:  # noqa: BLE001
            if strict:
                raise VisionServiceError(str(exc)) from exc
            return False

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
    def _call_model(
        api_url: str, model_name: str, prompt: str, images_b64: list[str],
    ) -> str:
        url = api_url.rstrip("/")
        payload = json.dumps({
            "model": model_name,
            "prompt": prompt,
            "images": images_b64,
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

    @staticmethod
    def _filter_candidates(text: str, candidate_tags: list[str]) -> list[str]:
        """Keep only tags that appear in the original candidate list."""
        stripped = text.strip()
        if stripped.upper() == "NONE":
            return []
        lower_map = {t.lower(): t for t in candidate_tags}
        matched: list[str] = []
        seen: set[str] = set()
        for raw in stripped.split(","):
            clean = raw.strip().lower()
            if clean in lower_map and clean not in seen:
                seen.add(clean)
                matched.append(lower_map[clean])
        return matched
