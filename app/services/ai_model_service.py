from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(slots=True)
class ModelInfo:
    name: str
    size: str = ""
    modified_at: str = ""


@dataclass(slots=True)
class ConnectionStatus:
    connected: bool = False
    message: str = ""
    loaded: bool = False


class AIModelProvider(ABC):
    """Base class for AI model providers (Ollama, OpenAI, etc.)."""

    @property
    @abstractmethod
    def supports_local_server(self) -> bool:
        """Whether this provider can be started/stopped locally."""
        ...

    @abstractmethod
    def check_connection(self, api_url: str) -> ConnectionStatus: ...

    @abstractmethod
    def check_model(self, api_url: str, model_name: str) -> ConnectionStatus: ...

    @abstractmethod
    def list_models(self, api_url: str) -> list[ModelInfo]: ...

    @abstractmethod
    def load_model(self, api_url: str, model_name: str) -> tuple[bool, str]: ...

    @abstractmethod
    def unload_model(self, api_url: str, model_name: str) -> tuple[bool, str]: ...

    @abstractmethod
    def start_server(self) -> tuple[bool, str]: ...

    @abstractmethod
    def stop_server(self) -> tuple[bool, str]: ...


class OllamaProvider(AIModelProvider):
    _TIMEOUT = 5

    @property
    def supports_local_server(self) -> bool:
        return True

    def check_connection(self, api_url: str) -> ConnectionStatus:
        try:
            url = api_url.rstrip("/")
            req = urllib.request.Request(f"{url}/", method="GET")
            with urllib.request.urlopen(req, timeout=self._TIMEOUT) as resp:
                if resp.status == 200:
                    body = resp.read().decode("utf-8", errors="replace").strip()
                    if "Ollama" in body:
                        return ConnectionStatus(connected=True, message="Ollama is running")
                    return ConnectionStatus(
                        connected=False,
                        message=f"Port is open but response is not Ollama: {body[:80]}",
                    )
        except (urllib.error.URLError, OSError) as exc:
            return ConnectionStatus(connected=False, message=f"Connection failed: {exc}")
        return ConnectionStatus(connected=False, message="Unexpected response")

    def check_model(self, api_url: str, model_name: str) -> ConnectionStatus:
        if not model_name.strip():
            return ConnectionStatus(connected=False, message="No model selected")
        try:
            url = api_url.rstrip("/")
            payload = json.dumps({"name": model_name}).encode("utf-8")
            req = urllib.request.Request(
                f"{url}/api/show", data=payload, method="POST",
            )
            req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, timeout=self._TIMEOUT) as resp:
                if resp.status != 200:
                    return ConnectionStatus(connected=False, message="Unexpected response")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return ConnectionStatus(
                    connected=False,
                    message=f"Model '{model_name}' not found locally",
                )
            return ConnectionStatus(
                connected=False,
                message=f"Model check failed: HTTP {exc.code}",
            )
        except (urllib.error.URLError, OSError) as exc:
            return ConnectionStatus(connected=False, message=f"Server not reachable: {exc}")

        is_loaded = self._is_model_loaded(api_url, model_name)
        if is_loaded:
            return ConnectionStatus(
                connected=True, loaded=True,
                message=f"Model '{model_name}' is running",
            )
        return ConnectionStatus(
            connected=True, loaded=False,
            message=f"Model '{model_name}' available (not loaded)",
        )

    def _is_model_loaded(self, api_url: str, model_name: str) -> bool:
        try:
            url = api_url.rstrip("/")
            req = urllib.request.Request(f"{url}/api/ps", method="GET")
            with urllib.request.urlopen(req, timeout=self._TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                models = data.get("models") or []
                normalized = self._normalize_model_name(model_name)
                for m in models:
                    running = self._normalize_model_name(m.get("name", ""))
                    if running == normalized:
                        return True
        except Exception:  # noqa: BLE001
            pass
        return False

    @staticmethod
    def _normalize_model_name(name: str) -> str:
        return name if ":" in name else f"{name}:latest"

    def load_model(self, api_url: str, model_name: str) -> tuple[bool, str]:
        try:
            url = api_url.rstrip("/")
            payload = json.dumps({
                "model": model_name,
                "stream": False,
                "keep_alive": -1,
            }).encode("utf-8")
            req = urllib.request.Request(f"{url}/api/generate", data=payload, method="POST")
            req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, timeout=600) as resp:
                if resp.status == 200:
                    return True, f"Model '{model_name}' loaded"
        except urllib.error.HTTPError as exc:
            return False, f"Load failed: HTTP {exc.code}"
        except (urllib.error.URLError, OSError) as exc:
            return False, f"Load failed: {exc}"
        return False, "Unexpected response"

    def unload_model(self, api_url: str, model_name: str) -> tuple[bool, str]:
        try:
            url = api_url.rstrip("/")
            payload = json.dumps({
                "model": model_name,
                "stream": False,
                "keep_alive": 0,
            }).encode("utf-8")
            req = urllib.request.Request(f"{url}/api/generate", data=payload, method="POST")
            req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.status == 200:
                    return True, f"Model '{model_name}' unloaded"
        except urllib.error.HTTPError as exc:
            return False, f"Unload failed: HTTP {exc.code}"
        except (urllib.error.URLError, OSError) as exc:
            return False, f"Unload failed: {exc}"
        return False, "Unexpected response"

    def list_models(self, api_url: str) -> list[ModelInfo]:
        try:
            url = api_url.rstrip("/")
            req = urllib.request.Request(f"{url}/api/tags", method="GET")
            with urllib.request.urlopen(req, timeout=self._TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                models: list[ModelInfo] = []
                for m in data.get("models", []):
                    size_bytes = m.get("size", 0)
                    size_str = f"{size_bytes / (1024**3):.1f} GB" if size_bytes else ""
                    models.append(ModelInfo(
                        name=m.get("name", ""),
                        size=size_str,
                        modified_at=m.get("modified_at", ""),
                    ))
                return models
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            return []

    def start_server(self) -> tuple[bool, str]:
        try:
            if sys.platform == "win32":
                subprocess.Popen(
                    ["ollama", "serve"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
                )
            else:
                subprocess.Popen(
                    ["ollama", "serve"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            return True, "Ollama server starting..."
        except FileNotFoundError:
            return False, "Ollama not found. Please install Ollama first."
        except OSError as exc:
            return False, f"Failed to start Ollama: {exc}"

    def stop_server(self) -> tuple[bool, str]:
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/F", "/IM", "ollama.exe"],
                    capture_output=True, check=False,
                )
            else:
                subprocess.run(
                    ["pkill", "-f", "ollama"],
                    capture_output=True, check=False,
                )
            return True, "Ollama server stopped."
        except OSError as exc:
            return False, f"Failed to stop Ollama: {exc}"


_PROVIDERS: dict[str, type[AIModelProvider]] = {
    "ollama": OllamaProvider,
}


def get_provider(provider_name: str) -> AIModelProvider:
    cls = _PROVIDERS.get(provider_name.lower())
    if cls is None:
        raise ValueError(f"Unknown AI model provider: {provider_name}")
    return cls()


def available_provider_names() -> list[str]:
    return list(_PROVIDERS.keys())
