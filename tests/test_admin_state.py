import json
import os
import stat
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "hermes-xmpp"))

from xmpp_bridge.admin_state import AdminConfig, AdminState, AdminStateError, ConfigValidationError


OWNER = "Owner@Example.Com/phone"


def test_initial_owner_normalizes_and_snapshot_never_contains_token(tmp_path):
    state = AdminState(tmp_path / "admin.json", OWNER)

    config = state.load()

    assert config == AdminConfig(frozenset({"owner@example.com"}), frozenset(), None, None, None, False, 0)
    assert "token" not in repr(config).casefold()
    assert state.token() is None


def test_mutation_normalizes_jids_and_advances_revision(tmp_path):
    state = AdminState(tmp_path / "admin.json", OWNER)

    updated = state.mutate(lambda config: config.with_changes(
        owners={"Owner@Example.Com", "Second@Example.Com/mobile"},
        trusted_jids={"Trusted@Example.Com/desktop"},
        model="model-a",
        endpoint="https://llm.example.com/v1",
    ))

    assert updated.owners == frozenset({"owner@example.com", "second@example.com"})
    assert updated.trusted_jids == frozenset({"trusted@example.com"})
    assert updated.revision == 1
    assert AdminState(tmp_path / "admin.json", OWNER).load() == updated


@pytest.mark.parametrize("endpoint", ["http://example.com", "ftp://example.com", "https://", "https://example.com user"])
def test_invalid_endpoint_is_rejected(tmp_path, endpoint):
    state = AdminState(tmp_path / "admin.json", OWNER)

    with pytest.raises(ConfigValidationError):
        state.mutate(lambda config: config.with_changes(endpoint=endpoint))


def test_https_or_loopback_endpoint_is_allowed(tmp_path):
    state = AdminState(tmp_path / "admin.json", OWNER)

    assert state.mutate(lambda config: config.with_changes(endpoint="https://provider.example/v1")).endpoint == "https://provider.example/v1"
    assert state.mutate(lambda config: config.with_changes(endpoint="http://127.0.0.1:8000/v1")).endpoint == "http://127.0.0.1:8000/v1"


def test_cannot_remove_last_owner_or_persist_empty_owners(tmp_path):
    state = AdminState(tmp_path / "admin.json", OWNER)

    with pytest.raises(ConfigValidationError):
        state.mutate(lambda config: config.with_changes(owners=set()))

    (tmp_path / "invalid.json").write_text('{"version":1,"revision":0,"owners":[],"trusted_jids":[],"model":null,"endpoint":null,"token":null}', encoding="utf-8")
    with pytest.raises(ConfigValidationError):
        AdminState(tmp_path / "invalid.json", OWNER).load()


def test_set_token_persists_secret_but_exposes_only_mask(tmp_path):
    state = AdminState(tmp_path / "admin.json", OWNER)

    public = state.set_token("correct horse battery staple")

    assert public.token_present is True
    assert public.token_mask is not None and "correct" not in public.token_mask
    assert "correct" not in repr(public)
    assert state.token() == "correct horse battery staple"
    assert AdminState(tmp_path / "admin.json", OWNER).token() == "correct horse battery staple"


def test_atomic_write_uses_private_mode_fsync_and_replace(tmp_path, monkeypatch):
    path = tmp_path / "nested" / "admin.json"
    state = AdminState(path, OWNER)
    calls = {"fsync": 0, "replace": []}
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd):
        calls["fsync"] += 1
        return real_fsync(fd)

    def replace(source, destination):
        calls["replace"].append((Path(source), Path(destination)))
        return real_replace(source, destination)

    monkeypatch.setattr("xmpp_bridge.admin_state.os.fsync", fsync)
    monkeypatch.setattr("xmpp_bridge.admin_state.os.replace", replace)
    state.load()

    assert calls["replace"][-1][0].parent == path.parent
    assert calls["replace"][-1][1] == path
    assert calls["fsync"] >= 1
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_symlink_and_nonregular_state_fail_closed(tmp_path):
    target = tmp_path / "target"
    target.write_text("protected", encoding="utf-8")
    link = tmp_path / "admin.json"
    try:
        link.symlink_to(target)
    except OSError as error:
        pytest.skip(str(error))

    with pytest.raises(AdminStateError):
        AdminState(link, OWNER).load()
    assert target.read_text(encoding="utf-8") == "protected"


def test_symlink_parent_fails_before_creating_state_or_lock(tmp_path):
    external = tmp_path / "external"
    external.mkdir()
    parent = tmp_path / "state"
    try:
        parent.symlink_to(external, target_is_directory=True)
    except OSError as error:
        pytest.skip(str(error))

    with pytest.raises(AdminStateError):
        AdminState(parent / "admin.json", OWNER).load()
    assert list(external.iterdir()) == []


def test_corrupt_and_interrupted_files_fail_closed_without_overwrite(tmp_path):
    path = tmp_path / "admin.json"
    path.write_text("{bad json", encoding="utf-8")

    with pytest.raises(ConfigValidationError):
        AdminState(path, OWNER).load()
    assert path.read_text(encoding="utf-8") == "{bad json"
    assert list(tmp_path.glob("*.tmp")) == []


def test_mutate_reloads_current_disk_state_to_avoid_lost_updates(tmp_path):
    path = tmp_path / "admin.json"
    first, second = AdminState(path, OWNER), AdminState(path, OWNER)
    first.load()
    first.mutate(lambda config: config.with_changes(model="one"))

    updated = second.mutate(lambda config: config.with_changes(endpoint="https://two.example/v1"))

    assert updated.model == "one"
    assert updated.endpoint == "https://two.example/v1"
    assert updated.revision == 2
