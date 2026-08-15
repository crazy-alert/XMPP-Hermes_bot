"""Persistent legacy OMEMO support for the XMPP transport."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Any

from omemo.storage import Just, Maybe, Nothing, Storage
from omemo.types import JSONType
from slixmpp.plugins import register_plugin
from slixmpp_omemo import TrustLevel, XEP_0384


class _JsonStorage(Storage):
    """Small private durable store for the OMEMO identity and sessions."""

    def __init__(self, path: Path) -> None:
        super().__init__()
        self._path = Path(path)
        self._data: dict[str, JSONType] = {}
        try:
            self._data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            pass
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise RuntimeError("invalid OMEMO state") from exc
        if not isinstance(self._data, dict):
            raise RuntimeError("invalid OMEMO state")

    async def _load(self, key: str) -> Maybe[JSONType]:
        return Just(self._data[key]) if key in self._data else Nothing()

    async def _store(self, key: str, value: JSONType) -> None:
        self._data[key] = value
        self._write()

    async def _delete(self, key: str) -> None:
        self._data.pop(key, None)
        self._write()

    def _write(self) -> None:
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=self._path.parent, prefix=f".{self._path.name}.", suffix=".tmp", delete=False
            ) as temporary:
                temporary_name = temporary.name
                if os.name != "nt":
                    os.chmod(temporary_name, 0o600)
                json.dump(self._data, temporary, ensure_ascii=False, separators=(",", ":"))
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_name, self._path)
            temporary_name = None
        finally:
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name)
                except FileNotFoundError:
                    pass


class HermesXEP0384(XEP_0384):
    """OMEMO plugin with durable state and trust-on-first-use for a service bot."""

    default_config = {"json_file_path": None, "fallback_message": "This message is OMEMO encrypted."}

    def plugin_init(self) -> None:
        if not self.json_file_path:
            raise RuntimeError("OMEMO state path is required")
        self._storage = _JsonStorage(Path(self.json_file_path))
        super().plugin_init()

    @property
    def storage(self) -> Storage:
        return self._storage

    @property
    def _btbv_enabled(self) -> bool:
        return True

    async def _devices_blindly_trusted(self, devices: Any, identifier: str | None) -> None:
        return None

    async def _prompt_manual_trust(self, devices: Any, identifier: str | None) -> None:
        manager = await self.get_session_manager()
        for device in devices:
            await manager.set_trust(device.bare_jid, device.identity_key, TrustLevel.TRUSTED.value)


register_plugin(HermesXEP0384)
