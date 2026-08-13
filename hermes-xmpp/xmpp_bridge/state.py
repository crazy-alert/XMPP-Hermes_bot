"""Durable private-room state kept independently from the XMPP runtime."""

from __future__ import annotations

from datetime import UTC, datetime
import json
import os
from pathlib import Path
import tempfile

from .policy import normalize_bare_jid


class RoomState:
    """Store normalized private-room JIDs in a small, private JSON file."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._rooms = frozenset()
        self._has_valid_state = False

    def load(self) -> frozenset[str]:
        """Load rooms, quarantining malformed files without losing known state."""
        while True:
            if not self.path.exists():
                return self._rooms if self._has_valid_state else frozenset()

            contents = self.path.read_bytes()
            try:
                text = contents.decode("utf-8")
                payload = json.loads(text)
                rooms = self._parse_payload(payload)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
                if not self._quarantine_corrupt_file(contents):
                    continue
                return self._rooms if self._has_valid_state else frozenset()

            self._rooms = rooms
            self._has_valid_state = True
            return rooms

    def add(self, room_jid: str) -> bool:
        """Persist a room, returning whether the state changed."""
        room = normalize_bare_jid(room_jid)
        self._ensure_loaded()
        updated = self._rooms | {room}
        if updated == self._rooms:
            return False
        self._write(updated)
        self._rooms = updated
        self._has_valid_state = True
        return True

    def remove(self, room_jid: str) -> bool:
        """Remove a room, returning whether the state changed."""
        room = normalize_bare_jid(room_jid)
        self._ensure_loaded()
        if room not in self._rooms:
            return False
        updated = self._rooms - {room}
        self._write(updated)
        self._rooms = updated
        self._has_valid_state = True
        return True

    def _ensure_loaded(self) -> None:
        if not self._has_valid_state:
            self.load()

    @staticmethod
    def _parse_payload(payload: object) -> frozenset[str]:
        if not isinstance(payload, dict) or set(payload) != {"version", "rooms"}:
            raise ValueError("invalid room state schema")
        if type(payload["version"]) is not int or payload["version"] != 1:
            raise ValueError("unsupported room state version")
        raw_rooms = payload["rooms"]
        if not isinstance(raw_rooms, list) or not all(isinstance(room, str) for room in raw_rooms):
            raise ValueError("invalid room list")
        return frozenset(normalize_bare_jid(room) for room in raw_rooms)

    def _write(self, rooms: frozenset[str]) -> None:
        parent = self.path.parent
        if not parent.exists():
            parent.mkdir(mode=0o700, parents=True)
            if os.name != "nt":
                os.chmod(parent, 0o700)

        contents = json.dumps(
            {"version": 1, "rooms": sorted(rooms)}, separators=(",", ":"), ensure_ascii=False
        ) + "\n"
        temp_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=parent, prefix=f".{self.path.name}.", suffix=".tmp", delete=False
            ) as temporary:
                temp_name = temporary.name
                if os.name != "nt":
                    os.chmod(temp_name, 0o600)
                temporary.write(contents)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temp_name, self.path)
            temp_name = None
        finally:
            if temp_name is not None:
                try:
                    os.unlink(temp_name)
                except FileNotFoundError:
                    pass

    def _quarantine_corrupt_file(self, expected_contents: bytes) -> bool:
        if not self.path.exists():
            return True
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        suffix = 0
        while True:
            discriminator = "" if suffix == 0 else f"-{suffix}"
            candidate = self.path.with_name(f"{self.path.name}.corrupt-{stamp}{discriminator}")
            try:
                os.link(self.path, candidate)
            except FileExistsError:
                suffix += 1
                continue
            break

        if candidate.read_bytes() != expected_contents:
            os.unlink(candidate)
            return False

        staging_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=self.path.parent,
                prefix=f".{self.path.name}.quarantine-",
                suffix=".tmp",
                delete=False,
            ) as staging:
                staging_name = staging.name
            try:
                os.replace(self.path, staging_name)
            except OSError:
                os.unlink(candidate)
                raise

            if os.path.samefile(staging_name, candidate):
                os.unlink(staging_name)
            else:
                try:
                    os.link(staging_name, self.path)
                except FileExistsError:
                    pass
                os.unlink(staging_name)
            staging_name = None
        finally:
            if staging_name is not None:
                try:
                    os.unlink(staging_name)
                except FileNotFoundError:
                    pass
        return True
