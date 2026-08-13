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

    assert state.add("Zulu@Conference.Example.Com/desktop") is True
    assert state.add("alpha@conference.example.com") is True

    assert state.load() == frozenset({"alpha@conference.example.com", "zulu@conference.example.com"})
    assert path.read_text(encoding="utf-8") == (
        '{"version":1,"rooms":["alpha@conference.example.com","zulu@conference.example.com"]}\n'
    )


def test_duplicate_add_and_absent_remove_do_not_rewrite_state_file(tmp_path):
    path = tmp_path / "rooms.json"
    state = RoomState(path)
    assert state.add("room@conference.example.com") is True
    before = path.read_bytes()

    assert state.add("ROOM@CONFERENCE.EXAMPLE.COM/resource") is False
    assert state.remove("other@conference.example.com") is False

    assert path.read_bytes() == before
    assert state.remove("room@conference.example.com") is True
    assert state.load() == frozenset()


@pytest.mark.parametrize("value", ["", " ", "not-a-jid", "@conference.example.com", "room@"])
def test_add_and_remove_reject_invalid_room_jids(tmp_path, value):
    state = RoomState(tmp_path / "rooms.json")

    with pytest.raises(ValueError):
        state.add(value)
    with pytest.raises(ValueError):
        state.remove(value)


def test_write_creates_restrictive_parent_and_state_file(tmp_path):
    path = tmp_path / "private" / "rooms.json"

    assert RoomState(path).add("room@conference.example.com") is True
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

    assert RoomState(path).add("room@conference.example.com") is True
    source, destination = calls["replace"].pop()
    assert source.parent == path.parent
    assert destination == path
    assert calls["fsync"] == 1


def test_failed_atomic_replace_keeps_original_file_and_in_memory_state(tmp_path, monkeypatch):
    path = tmp_path / "rooms.json"
    state = RoomState(path)
    assert state.add("room@conference.example.com") is True
    before = path.read_bytes()

    def fail_replace(source, destination):
        raise OSError("disk error")

    monkeypatch.setattr("xmpp_bridge.state.os.replace", fail_replace)
    with pytest.raises(OSError, match="disk error"):
        state.add("other@conference.example.com")

    assert path.read_bytes() == before
    assert state.load() == frozenset({"room@conference.example.com"})
    assert list(path.parent.glob("*.tmp")) == []


def test_write_does_not_report_failure_after_successful_replace(tmp_path, monkeypatch):
    path = tmp_path / "rooms.json"
    state = RoomState(path)
    assert state.add("room@conference.example.com") is True

    replaced = False
    real_replace = os.replace

    def track_replace(source, destination):
        nonlocal replaced
        real_replace(source, destination)
        replaced = True

    def fail_chmod(*args):
        if replaced:
            raise OSError("must not chmod after replace")

    monkeypatch.setattr("xmpp_bridge.state.os.replace", track_replace)
    monkeypatch.setattr("xmpp_bridge.state.os.chmod", fail_chmod)

    assert state.add("other@conference.example.com") is True
    assert state.load() == frozenset({"room@conference.example.com", "other@conference.example.com"})


def test_transient_read_error_preserves_file_and_valid_in_memory_state(tmp_path, monkeypatch):
    path = tmp_path / "rooms.json"
    state = RoomState(path)
    assert state.add("room@conference.example.com") is True
    before = path.read_bytes()
    real_read_bytes = Path.read_bytes

    def fail_read(self, *args, **kwargs):
        raise OSError("temporary I/O failure")

    monkeypatch.setattr(Path, "read_bytes", fail_read)
    with pytest.raises(OSError, match="temporary I/O failure"):
        state.load()

    assert real_read_bytes(path) == before
    assert state._rooms == frozenset({"room@conference.example.com"})
    assert list(tmp_path.glob("rooms.json.corrupt-*")) == []


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


def test_corrupt_json_retries_quarantine_name_after_racing_reservation(tmp_path, monkeypatch):
    path = tmp_path / "rooms.json"
    path.write_text("{not json", encoding="utf-8")
    real_link = os.link
    calls = 0

    def race_link(source, candidate):
        nonlocal calls
        calls += 1
        if calls == 1:
            Path(candidate).write_text("racer evidence", encoding="utf-8")
            raise FileExistsError(candidate)
        return real_link(source, candidate)

    monkeypatch.setattr("xmpp_bridge.state.os.link", race_link)

    assert RoomState(path).load() == frozenset()
    quarantined = sorted(tmp_path.glob("rooms.json.corrupt-*"))
    assert [item.read_text(encoding="utf-8") for item in quarantined] == ["racer evidence", "{not json"]
    assert not path.exists()


def test_quarantine_does_not_delete_state_replaced_during_move(tmp_path, monkeypatch):
    path = tmp_path / "rooms.json"
    path.write_text("{not json", encoding="utf-8")
    replacement = '{"version":1,"rooms":["new@conference.example.com"]}\n'
    real_replace = os.replace
    raced = False

    def race_replace(source, destination):
        nonlocal raced
        if Path(source) == path and not raced:
            raced = True
            concurrent = tmp_path / "concurrent.json"
            concurrent.write_text(replacement, encoding="utf-8")
            real_replace(concurrent, path)
        real_replace(source, destination)

    monkeypatch.setattr("xmpp_bridge.state.os.replace", race_replace)

    assert RoomState(path).load() == frozenset()
    quarantined = list(tmp_path.glob("rooms.json.corrupt-*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8") == "{not json"
    assert path.read_text(encoding="utf-8") == replacement


def test_quarantine_reloads_state_replaced_before_link(tmp_path, monkeypatch):
    path = tmp_path / "rooms.json"
    path.write_text("{not json", encoding="utf-8")
    replacement = '{"version":1,"rooms":["new@conference.example.com"]}\n'
    real_link = os.link
    real_replace = os.replace
    raced = False

    def race_link(source, destination):
        nonlocal raced
        if Path(source) == path and not raced:
            raced = True
            concurrent = tmp_path / "concurrent.json"
            concurrent.write_text(replacement, encoding="utf-8")
            real_replace(concurrent, path)
        real_link(source, destination)

    monkeypatch.setattr("xmpp_bridge.state.os.link", race_link)

    assert RoomState(path).load() == frozenset({"new@conference.example.com"})
    assert path.read_text(encoding="utf-8") == replacement
    assert list(tmp_path.glob("rooms.json.corrupt-*")) == []


def test_failed_quarantine_move_removes_empty_reservation(tmp_path, monkeypatch):
    path = tmp_path / "rooms.json"
    path.write_text("{not json", encoding="utf-8")

    def fail_replace(source, destination):
        raise OSError("quarantine move failed")

    monkeypatch.setattr("xmpp_bridge.state.os.replace", fail_replace)

    with pytest.raises(OSError, match="quarantine move failed"):
        RoomState(path).load()

    assert path.read_text(encoding="utf-8") == "{not json"
    assert list(tmp_path.glob("rooms.json.corrupt-*")) == []


def test_failed_load_preserves_previously_valid_in_memory_rooms(tmp_path):
    path = tmp_path / "rooms.json"
    state = RoomState(path)
    assert state.add("room@conference.example.com") is True
    path.write_text('{"version":1,"rooms":[42]}', encoding="utf-8")

    assert state.load() == frozenset({"room@conference.example.com"})
    assert not path.exists()
    assert len(list(tmp_path.glob("rooms.json.corrupt-*"))) == 1


@pytest.mark.parametrize(
    "payload",
    [
        "[]",
        '{"version":"1","rooms":[]}',
        '{"version":1,"rooms":"room@conference.example.com"}',
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
        '{"version":1,"rooms":["Room@Conference.Example.Com/desktop","room@conference.example.com"]}',
        encoding="utf-8",
    )

    assert RoomState(path).load() == frozenset({"room@conference.example.com"})
