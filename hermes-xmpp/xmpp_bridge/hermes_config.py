"""Safe synchronization of XMPP-owned AI settings with Hermes runtime files."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
from urllib.parse import urlparse

import yaml


_TOKEN_ENV_KEY = "HERMES_XMPP_AI_API_KEY"
_LEAF_ROUTES = {
    ("chat", "completions"),
    ("images", "generations"),
    ("responses",),
    ("messages",),
}


class RuntimeConfigError(RuntimeError):
    """The Hermes runtime configuration cannot be read or safely updated."""


def validate_api_base_url(value: object) -> str:
    """Validate an OpenAI-compatible API base URL, never a concrete method."""
    if not isinstance(value, str) or not (url := value.strip()) or any(char.isspace() for char in value):
        raise RuntimeConfigError("API base URL is invalid")
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError as error:
        raise RuntimeConfigError("API base URL is invalid") from error
    loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RuntimeConfigError("API base URL is invalid")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise RuntimeConfigError("API base URL must use HTTPS or loopback HTTP")
    if port is not None and not 1 <= port <= 65535:
        raise RuntimeConfigError("API base URL is invalid")
    path = parsed.path.rstrip("/")
    segments = tuple(segment.casefold() for segment in path.split("/") if segment)
    if any(segments[-len(route):] == route for route in _LEAF_ROUTES):
        raise RuntimeConfigError("API base URL must not include a concrete API route")
    return f"{parsed.scheme}://{parsed.netloc}{path}"


class HermesRuntimeConfig:
    """Atomically maintain the Hermes files owned by the service user."""

    def __init__(self, home: Path) -> None:
        self.home = Path(home)
        self.config_path = self.home / "config.yaml"
        self.env_path = self.home / ".env"

    def apply(self, config, token: str | None) -> bool:
        """Write a complete chat configuration; incomplete state is left untouched."""
        model = getattr(config, "model", None)
        endpoint = getattr(config, "endpoint", None)
        if not (isinstance(model, str) and model.strip() and isinstance(endpoint, str) and token):
            return False
        if not isinstance(token, str) or any(char in token for char in "\r\n\x00"):
            raise RuntimeConfigError("API token is invalid")
        endpoint = validate_api_base_url(endpoint)
        document = self._read_yaml()
        providers = document.setdefault("providers", {})
        if not isinstance(providers, dict):
            raise RuntimeConfigError("Hermes providers configuration is invalid")
        model_section = document.setdefault("model", {})
        if not isinstance(model_section, dict):
            raise RuntimeConfigError("Hermes model configuration is invalid")
        providers["xmpp-ai"] = {
            "name": "XMPP AI",
            "base_url": endpoint,
            "model": model.strip(),
            "key_env": _TOKEN_ENV_KEY,
            "discover_models": False,
        }
        model_section["provider"] = "xmpp-ai"
        model_section["default"] = model.strip()
        model_section["base_url"] = endpoint
        model_section["key_env"] = _TOKEN_ENV_KEY
        env_contents = self._updated_env_contents(_TOKEN_ENV_KEY, token)
        self._atomic_write(self.config_path, yaml.safe_dump(document, allow_unicode=True, sort_keys=False))
        self._atomic_write(self.env_path, env_contents)
        return True

    def set_image_model(self, model: str) -> None:
        if not isinstance(model, str) or not (model := model.strip()) or len(model) > 512:
            raise RuntimeConfigError("image model is invalid")
        document = self._read_yaml()
        image_gen = document.setdefault("image_gen", {})
        if not isinstance(image_gen, dict):
            raise RuntimeConfigError("Hermes image configuration is invalid")
        image_gen["provider"] = "xmpp-ai"
        image_gen["model"] = model
        self._atomic_write(self.config_path, yaml.safe_dump(document, allow_unicode=True, sort_keys=False))

    def image_status(self) -> str:
        document = self._read_yaml()
        image_gen = document.get("image_gen", {})
        model = image_gen.get("model") if isinstance(image_gen, dict) else None
        return f"Модель изображений: {model}" if isinstance(model, str) and model else "Модель изображений не задана."

    def _read_yaml(self) -> dict:
        self._require_safe_home()
        if not self.config_path.exists():
            return {}
        self._require_regular_file(self.config_path)
        try:
            document = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as error:
            raise RuntimeConfigError("Hermes config cannot be read") from error
        if document is None:
            return {}
        if not isinstance(document, dict):
            raise RuntimeConfigError("Hermes config must be a mapping")
        return document

    def _updated_env_contents(self, key: str, value: str) -> str:
        self._require_safe_home()
        if self.env_path.exists():
            self._require_regular_file(self.env_path)
            try:
                lines = self.env_path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeError) as error:
                raise RuntimeConfigError("Hermes environment cannot be read") from error
        else:
            lines = []
        prefix = f"{key}="
        remaining = [line for line in lines if not line.startswith(prefix)]
        remaining.append(prefix + value)
        return "\n".join(remaining) + "\n"

    def _require_safe_home(self) -> None:
        if self.home.is_symlink() or not self.home.is_dir():
            raise RuntimeConfigError("Hermes home is unsafe")

    @staticmethod
    def _require_regular_file(path: Path) -> None:
        if path.is_symlink() or not path.is_file():
            raise RuntimeConfigError("Hermes configuration path is unsafe")

    @staticmethod
    def _atomic_write(path: Path, contents: str) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
                output.write(contents)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
            temporary = ""
        except OSError as error:
            raise RuntimeConfigError("Hermes configuration cannot be written") from error
        finally:
            if temporary:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
