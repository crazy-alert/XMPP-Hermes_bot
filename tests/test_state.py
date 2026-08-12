import json
import os
import stat
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "hermes-xmpp"))

from xmpp_bridge.state import RoomState


def test_load_missing_file_returns_empty_without_creating_it(tmp_path):
    path = tmp_path / "private" / "rooms.json"

    assert RoomState(path).load() == frozenset()
    assert not path.exists()
    assert not path.parent.exists()


def test_add_canonicalizes_resource_and_writes_sorted_schema(tmp_path):
    path = tmp_path / "rooms.json"
    state = RoomState(path)

    assert state.add("Zulu@Conference.Aversa.Run/desktop") is True
    assert state.add("alpha@conference.aversa.run") is True

    assert state.load() == frozenset({"alpha@conference.aversa.run", "zulu@conference.aversa.run"})
    assert path.read_text(encoding="utf-8") == (
        '{"version":1,"rooms":["alpha@conference.aversa.run","zulu@conference.aversa.run"]}\n'
    )


def test_duplicate_add_and_absent_remove_do_not_rewrite_state_file(tmp_path):
    path = tmp_path / "rooms.json"
    state = RoomState(path)
    assert state.add("room@conference.aversa.run") is True
    before = path.read_bytes()

    assert state.add("ROOM@CONFERENCE.AVERSA.RUN/resource") is False
    assert state.remove("other@conference.aversa.run") is False

    assert path.read_bytes() == before
    assert state.remove("room@conference.aversa.run") is True
    assert state.load() == frozenset()


@pytest.mark.parametrize("value", ["", " ", "not-a-jid", "@conference.aversa.run", "room@"])
def test_add_and_remove_reject_invalid_room_jids(tmp_path, value):
    state = RoomState(tmp_path / "rooms.json")

    with pytest.raises(ValueError):
        state.add(value)
    with pytest.raises(ValueError):
        state.remove(value)


def test_write_creates_restrictive_parent_and_state_file(tmp_path):
    path = tmp_path / "private" / "rooms.json"

    assert RoomState(path).add("room@conference.aversa.run") is True
    assert path.exists()
    if os.name == "nt":
        pytest.skip("POSIX permission bits are not enforceable on Windows")
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_write_uses_same_directory_tempfile_flushes_fsyncs_and_replaces(tmp_path, monkeypatch):
    path = tmp_path / "private" / "rooms.json"
    calls = {"fsync": 0, "replace": []}
    real_fsync = os.fsync
    real_replace = os.replace

    def tracking_fsync(fd):
        calls["fsync"] += 1
        return real_fsync(fd)

    def tracking_replace(source, destination):
        calls["replace"].append((Path(source), Path(destination)))
        return real_replace(source, destination)

    monkeypatch.setattr("xmpp_bridge.state.os.fsync", tracking_fsync)
    monkeypatch.setattr("xmpp_bridge.state.os.replace", tracking_replace)

    assert RoomState(path).add("room@conference.aversa.run") is True
    source, destination = calls["replace"].pop()
    assert source.parent == path.parent
    assert destination == path
    assert calls["fsync"] == 1


def test_failed_atomic_replace_keeps_original_file_and_in_memory_state(tmp_path, monkeypatch):
    path = tmp_path / "rooms.json"
    state = RoomState(path)
    assert state.add("room@conference.aversa.run") is True
    before = path.read_bytes()

    def fail_replace(source, destination):
        raise OSError("disk error")

    monkeypatch.setattr("xmpp_bridge.state.os.replace", fail_replace)
    with pytest.raises(OSError, match="disk error"):
        state.add("other@conference.aversa.run")

    assert path.read_bytes() == before
    assert state.load() == frozenset({"room@conference.aversa.run"})
    assert list(path.parent.glob("*.tmp")) == []


def test_corrupt_json_is_quarantined_and_empty_when_no_valid_state(tmp_path):
    path = tmp_path / "rooms.json"
    path.write_text("{not json", encoding="utf-8")

    assert RoomState(path).load() == frozenset()

    quarantined = list(tmp_path.glob("rooms.json.corrupt-*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8") == "{not json"
    assert not path.exists()


def test_corrupt_json_does_not_replace_existing_quarantine(tmp_path):
    path = tmp_path / "rooms.json"
    existing = tmp_path / "rooms.json.corrupt-20990101T000000Z"
    existing.write_text("older evidence", encoding="utf-8")
    path.write_text("[]", encoding="utf-8")

    assert RoomState(path).load() == frozenset()
    assert existing.read_text(encoding="utf-8") == "older evidence"
    assert len(list(tmp_path.glob("rooms.json.corrupt-*"))) == 2


def test_failed_load_preserves_previously_valid_in_memory_rooms(tmp_path):
    path = tmp_path / "rooms.json"
    state = RoomState(path)
    assert state.add("room@conference.aversa.run") is True
    path.write_text('{"version":1,"rooms":[42]}', encoding="utf-8")

    assert state.load() == frozenset({"room@conference.aversa.run"})
    assert not path.exists()
    assert len(list(tmp_path.glob("rooms.json.corrupt-*"))) == 1


@pytest.mark.parametrize(
    "payload",
    [
        "[]",
        '{"version":"1","rooms":[]}',
        '{"version":1,"rooms":"room@conference.aversa.run"}',
        '{"version":1,"rooms":[42]}',
        '{"version":1,"rooms":["not-a-jid"]}',
        '{"version":1,"rooms":[],"extra":true}',
    ],
)
def test_load_quarantines_invalid_schema(tmp_path, payload):
    path = tmp_path / "rooms.json"
    path.write_text(payload, encoding="utf-8")

    assert RoomState(path).load() == frozenset()
    assert not path.exists()
    assert len(list(tmp_path.glob("rooms.json.corrupt-*"))) == 1


def test_load_canonicalizes_duplicate_and_resource_bearing_room_entries(tmp_path):
    path = tmp_path / "rooms.json"
    path.write_text(
        '{"version":1,"rooms":["Room@Conference.Aversa.Run/desktop","room@conference.aversa.run"]}',
        encoding="utf-8",
    )

    assert RoomState(path).load() == frozenset({"room@conference.aversa.run"})
