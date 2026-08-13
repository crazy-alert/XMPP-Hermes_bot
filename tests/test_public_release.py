from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "scripts/build-public-release.py"
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"(?im)^\s*(?:export\s+)?[A-Z][A-Z0-9_]*(?:PASSWORD|TOKEN|SECRET|API_KEY)[A-Z0-9_]*\s*=\s*[^\s#]+"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bxoxb-[A-Za-z0-9-]{20,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\b"),
)


def tracked_paths() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return [ROOT / raw.decode("utf-8") for raw in result.stdout.split(b"\0") if raw]


def tracked_text_files() -> list[Path]:
    paths = []
    for path in tracked_paths():
        if path.relative_to(ROOT).as_posix().startswith(".superpowers/"):
            continue
        data = path.read_bytes()
        if b"\0" not in data:
            data.decode("utf-8")
            paths.append(path)
    return paths


def private_identifiers() -> tuple[str, ...]:
    values = [line.strip() for line in os.environ.get("HERMES_PUBLIC_RELEASE_DENYLIST", "").splitlines()]
    file_name = os.environ.get("HERMES_PUBLIC_RELEASE_DENYLIST_FILE", "").strip()
    if file_name:
        path = Path(file_name).expanduser().resolve()
        if ROOT == path or ROOT in path.parents:
            relative = path.relative_to(ROOT).as_posix()
            ignored = subprocess.run(
                ["git", "check-ignore", "--no-index", "--quiet", relative], cwd=ROOT, check=False
            )
            if ignored.returncode != 0:
                raise ValueError("local release denylist inside the repository must be ignored")
        values.extend(path.read_text(encoding="utf-8").splitlines())
    return tuple(dict.fromkeys(value for value in values if value and not value.startswith("#")))


def files_matching(values: tuple[str, ...], files: list[Path]) -> list[str]:
    folded_values = tuple(value.casefold() for value in values)
    matches = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        folded = text.casefold()
        if any(value in folded for value in folded_values):
            matches.append(path.relative_to(ROOT).as_posix() if ROOT in path.parents else path.name)
    return matches


def test_tracked_public_text_files_contain_no_generic_secret_patterns() -> None:
    matches = []
    for path in tracked_text_files():
        text = path.read_text(encoding="utf-8")
        if any(pattern.search(text) for pattern in SECRET_PATTERNS):
            matches.append(path.relative_to(ROOT).as_posix())
    assert matches == []


def test_release_audit_requires_external_denylist() -> None:
    if os.environ.get("HERMES_RELEASE_AUDIT") != "1":
        pytest.skip("set HERMES_RELEASE_AUDIT=1 for the private pre-publication audit")
    if not private_identifiers():
        pytest.fail(
            "HERMES_RELEASE_AUDIT=1 requires a nonempty "
            "HERMES_PUBLIC_RELEASE_DENYLIST or HERMES_PUBLIC_RELEASE_DENYLIST_FILE"
        )
    assert files_matching(private_identifiers(), tracked_text_files()) == []


def test_env_template_has_exact_generic_values() -> None:
    values = {}
    for raw in (ROOT / "deploy/hermes.env.example").read_text(encoding="utf-8").splitlines():
        if raw and not raw.startswith("#"):
            key, value = raw.split("=", 1)
            values[key] = value
    assert values == {
        "HERMES_HOME": "/var/lib/hermes/.hermes",
        "XMPP_JID": "bot@example.com/Hermes",
        "XMPP_ALLOWED_USERS": "admin@example.com",
        "XMPP_NICK": "Hermes",
        "XMPP_STATE_PATH": "/var/lib/hermes/.hermes/xmpp/rooms.json",
    }


def test_optional_external_denylist_is_enforced_without_tracked_literals(tmp_path, monkeypatch) -> None:
    denylist = tmp_path / "private-release-denylist.txt"
    denylist.write_text("sensitive.internal\nadmin@sensitive.internal\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_PUBLIC_RELEASE_DENYLIST_FILE", str(denylist))

    assert private_identifiers() == ("sensitive.internal", "admin@sensitive.internal")
    sample = tmp_path / "candidate.txt"
    sample.write_text("connect to ADMIN@SENSITIVE.INTERNAL", encoding="utf-8")
    assert files_matching(private_identifiers(), [sample]) == ["candidate.txt"]


def make_release_source(tmp_path: Path, files: dict[str, str]) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=source, check=True)
    for relative, contents in files.items():
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=source, check=True)
    return source


def run_builder(source: Path, destination: Path, denylist: Path | None = None):
    command = [sys.executable, str(BUILDER), "--source", str(source), "--destination", str(destination)]
    if denylist is not None:
        command.extend(("--denylist-file", str(denylist)))
    return subprocess.run(command, text=True, capture_output=True, check=False)


def load_builder_module():
    spec = importlib.util.spec_from_file_location("public_release_builder_under_test", BUILDER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def safe_release_files() -> dict[str, str]:
    return {
        ".gitignore": ".superpowers/\n__pycache__/\n",
        "README.md": "Generic XMPP plugin\n",
        "deploy/hermes.env.example": "XMPP_JID=bot@example.com/Hermes\n",
        "hermes-xmpp/plugin.yaml": "name: xmpp-platform\n",
        "scripts/build-public-release.py": "# release builder\n",
        "tests/test_public_release.py": "def test_placeholder(): pass\n",
        "docs/superpowers/plans/internal.md": "development plan\n",
        ".superpowers/sdd/task-report.md": "development report\n",
    }


def external_denylist(tmp_path: Path) -> Path:
    path = tmp_path / "denylist.txt"
    path.write_text("sensitive.internal\n", encoding="utf-8")
    return path


def test_builder_requires_nonempty_external_denylist(tmp_path) -> None:
    source = make_release_source(tmp_path, safe_release_files())

    result = run_builder(source, tmp_path / "release")

    assert result.returncode != 0
    assert "denylist" in result.stderr.casefold()


def test_builder_rejects_repository_local_denylist_unless_ignored(tmp_path) -> None:
    source = make_release_source(tmp_path, safe_release_files())
    denylist = source / "local-denylist.txt"
    denylist.write_text("sensitive.internal\n", encoding="utf-8")

    rejected = run_builder(source, tmp_path / "rejected", denylist)
    assert rejected.returncode != 0
    assert "ignored" in rejected.stderr.casefold()

    with (source / ".gitignore").open("a", encoding="utf-8") as ignore:
        ignore.write("local-denylist.txt\n")
    accepted = run_builder(source, tmp_path / "accepted", denylist)
    assert accepted.returncode == 0, accepted.stderr


def test_builder_copies_exact_safe_tree_and_excludes_development_docs(tmp_path) -> None:
    source = make_release_source(tmp_path, safe_release_files())
    destination = tmp_path / "release"

    result = run_builder(source, destination, external_denylist(tmp_path))

    assert result.returncode == 0, result.stderr
    assert sorted(path.relative_to(destination).as_posix() for path in destination.rglob("*") if path.is_file()) == [
        ".gitignore",
        "README.md",
        "deploy/hermes.env.example",
        "hermes-xmpp/plugin.yaml",
        "scripts/build-public-release.py",
        "tests/test_public_release.py",
    ]
    assert not (destination / ".git").exists()
    assert not (destination / "docs/superpowers").exists()
    assert not (destination / ".superpowers").exists()


def test_builder_rejects_unexpected_tracked_path(tmp_path) -> None:
    files = safe_release_files()
    files["hermes-xmpp/private-notes.txt"] = "unexpected\n"
    source = make_release_source(tmp_path, files)

    result = run_builder(source, tmp_path / "release", external_denylist(tmp_path))

    assert result.returncode != 0
    assert "hermes-xmpp/private-notes.txt" in result.stderr


def test_builder_rejects_private_identifier_before_copy(tmp_path) -> None:
    files = safe_release_files()
    files["README.md"] = "connect to sensitive.internal\n"
    source = make_release_source(tmp_path, files)
    destination = tmp_path / "release"

    result = run_builder(source, destination, external_denylist(tmp_path))

    assert result.returncode != 0
    assert "README.md" in result.stderr
    assert not destination.exists()


def test_builder_rejects_nonempty_destination(tmp_path) -> None:
    source = make_release_source(tmp_path, safe_release_files())
    destination = tmp_path / "release"
    destination.mkdir()
    (destination / "keep.txt").write_text("keep", encoding="utf-8")

    result = run_builder(source, destination, external_denylist(tmp_path))

    assert result.returncode != 0
    assert (destination / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_builder_rejects_existing_empty_destination(tmp_path) -> None:
    source = make_release_source(tmp_path, safe_release_files())
    destination = tmp_path / "release"
    destination.mkdir()

    result = run_builder(source, destination, external_denylist(tmp_path))

    assert result.returncode != 0
    assert destination.is_dir()
    assert list(destination.iterdir()) == []


@pytest.mark.parametrize("target_exists", [True, False])
def test_builder_rejects_existing_or_dangling_destination_symlink_before_write(tmp_path, target_exists) -> None:
    source = make_release_source(tmp_path, safe_release_files())
    target = tmp_path / "external"
    if target_exists:
        target.mkdir()
    destination = tmp_path / "release"
    try:
        destination.symlink_to(target, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symlinks unavailable: {error}")

    result = run_builder(source, destination, external_denylist(tmp_path))

    assert result.returncode != 0
    assert destination.is_symlink()
    if target_exists:
        assert list(target.iterdir()) == []
    else:
        assert not target.exists()


def test_builder_rejects_nondirectory_destination_before_write(tmp_path) -> None:
    source = make_release_source(tmp_path, safe_release_files())
    destination = tmp_path / "release"
    destination.write_text("preserve", encoding="utf-8")

    result = run_builder(source, destination, external_denylist(tmp_path))

    assert result.returncode != 0
    assert destination.read_text(encoding="utf-8") == "preserve"


@pytest.mark.parametrize("attack", ["before_first_write", "mid_tree", "before_publish"])
def test_builder_destination_swap_or_injection_never_writes_external(tmp_path, monkeypatch, attack) -> None:
    source = make_release_source(tmp_path, safe_release_files())
    destination = tmp_path / "release"
    external = tmp_path / "external"
    external.mkdir()
    module = load_builder_module()

    if attack in {"before_first_write", "mid_tree"}:
        original_open = module.os.open
        output_opens = 0
        def attack_open(path, flags, *args, **kwargs):
            nonlocal output_opens
            if flags & module.os.O_WRONLY:
                output_opens += 1
                threshold = 1 if attack == "before_first_write" else 2
                if output_opens == threshold:
                    destination.symlink_to(external, target_is_directory=True)
            return original_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(module.os, "open", attack_open)
    else:
        original_copy = module.copy_release

        def inject_then_return(source_path, staging_path, files):
            original_copy(source_path, staging_path, files)
            destination.mkdir()
            (destination / "unexpected.txt").write_text("injected", encoding="utf-8")

        monkeypatch.setattr(module, "copy_release", inject_then_return)

    with pytest.raises(module.ReleaseError):
        module.build(source, destination, external_denylist(tmp_path))

    assert list(external.iterdir()) == []
    if destination.is_symlink():
        assert destination.resolve() == external.resolve()
    else:
        assert (destination / "unexpected.txt").read_text(encoding="utf-8") == "injected"


@pytest.mark.parametrize("replacement", ["symlink", "different_inode"])
def test_builder_rejects_source_replaced_after_validation_and_cleans_staging(tmp_path, monkeypatch, replacement) -> None:
    source = make_release_source(tmp_path, safe_release_files())
    destination = tmp_path / "release"
    module = load_builder_module()
    original_copy = module.copy_release

    def replace_then_copy(source_path, bound_destination, files):
        victim = source_path / "README.md"
        victim.unlink()
        if replacement == "symlink":
            try:
                victim.symlink_to(source_path / "deploy/hermes.env.example")
            except OSError as error:
                pytest.skip(f"symlinks unavailable: {error}")
        else:
            victim.write_text("different inode\n", encoding="utf-8")
        return original_copy(source_path, bound_destination, files)

    monkeypatch.setattr(module, "copy_release", replace_then_copy)

    with pytest.raises(module.ReleaseError):
        module.build(source, destination, external_denylist(tmp_path))
    assert not destination.exists()
    assert list(tmp_path.glob(".release.staging-*"))


@pytest.mark.parametrize("swap_point", ["after_check", "mid_cleanup"])
def test_cleanup_never_deletes_through_swapped_staging_path(tmp_path, monkeypatch, swap_point) -> None:
    module = load_builder_module()
    external = tmp_path / "external"
    external.mkdir()
    for name in ("first.txt", "second.txt"):
        (external / name).write_text("preserve", encoding="utf-8")
    staging = tmp_path / "staging"
    staging.mkdir()
    created = []
    for name in ("first.txt", "second.txt"):
        path = staging / name
        path.write_text("owned", encoding="utf-8")
        created.append(path)
    file_stat = staging.lstat()
    bound = module.BoundDestination(
        staging,
        module._identity(file_stat),
        None,
        tmp_path,
        module._stable_identity(tmp_path.lstat()),
        None,
        created,
        [],
    )
    original_check = module._check_bound
    checks = 0

    def swap_after_check(destination):
        nonlocal checks
        original_check(destination)
        checks += 1
        threshold = 1 if swap_point == "after_check" else 2
        if checks == threshold:
            staging.rename(tmp_path / f"displaced-{swap_point}")
            staging.symlink_to(external, target_is_directory=True)

    monkeypatch.setattr(module, "_check_bound", swap_after_check)

    module._cleanup_destination(bound)

    assert {(path.name, path.read_text(encoding="utf-8")) for path in external.iterdir()} == {
        ("first.txt", "preserve"),
        ("second.txt", "preserve"),
    }


@pytest.mark.parametrize("unsafe_kind", ["symlink", "directory"])
def test_builder_rejects_tracked_symlink_or_special_file(tmp_path, unsafe_kind) -> None:
    source = make_release_source(tmp_path, safe_release_files())
    target = source / "README.md"
    target.unlink()
    try:
        if unsafe_kind == "symlink":
            target.symlink_to(source / "deploy/hermes.env.example")
        else:
            target.mkdir()
    except OSError as error:
        pytest.skip(f"symlinks unavailable: {error}")

    result = run_builder(source, tmp_path / "release", external_denylist(tmp_path))

    assert result.returncode != 0
    assert "README.md" in result.stderr


def test_builder_rejects_generic_secret_pattern(tmp_path) -> None:
    files = safe_release_files()
    files["README.md"] = "API_TOKEN=not-for-publication\n"
    source = make_release_source(tmp_path, files)

    result = run_builder(source, tmp_path / "release", external_denylist(tmp_path))

    assert result.returncode != 0
    assert "README.md" in result.stderr


@pytest.mark.parametrize(
    "path",
    [
        ".private-release-denylist",
        ".superpowers/sdd/local/task-report.md",
        "tests/__pycache__/test_public_release.pyc",
        ".pytest_cache/v/cache/nodeids",
    ],
)
def test_local_audit_and_development_artifacts_are_gitignored(path) -> None:
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "--quiet", path],
        cwd=ROOT,
        check=False,
    )
    assert result.returncode == 0
