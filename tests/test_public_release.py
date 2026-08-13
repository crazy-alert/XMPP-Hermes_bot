from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_ROOT_FILES = frozenset(
    {".gitattributes", ".gitignore", "AGENTS.md", "DESIGN.md", "IMPLEMENTATION_PLAN.md", "README.md"}
)
PUBLIC_DIRECTORIES = ("deploy/", "docs/", "hermes-xmpp/", "tests/")
DEVELOPMENT_DIRECTORIES = (".superpowers/",)
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


def publication_paths() -> list[Path]:
    published = []
    for path in tracked_paths():
        relative = path.relative_to(ROOT).as_posix()
        if relative in PUBLIC_ROOT_FILES or relative.startswith(PUBLIC_DIRECTORIES):
            if "__pycache__" not in relative and not relative.endswith((".pyc", "-report.md")):
                published.append(path)
    return published


def publication_text_files() -> list[Path]:
    paths = []
    for path in publication_paths():
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


def test_tracked_text_files_contain_no_private_identifiers() -> None:
    assert files_matching(private_identifiers(), publication_text_files()) == []


def test_publication_text_files_contain_no_generic_secret_patterns() -> None:
    matches = []
    for path in publication_text_files():
        text = path.read_text(encoding="utf-8")
        if any(pattern.search(text) for pattern in SECRET_PATTERNS):
            matches.append(path.relative_to(ROOT).as_posix())
    assert matches == []


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


def test_publication_allowlist_excludes_development_reports_and_caches() -> None:
    published = [path.relative_to(ROOT).as_posix() for path in publication_text_files()]
    excluded = [path.relative_to(ROOT).as_posix() for path in tracked_paths() if path not in publication_paths()]

    assert published
    assert not any(path.startswith(".superpowers/") for path in published)
    assert not any("__pycache__" in path or path.endswith(".pyc") for path in published)
    assert not any(path.endswith("-report.md") for path in published)
    assert excluded
    assert all(path.startswith(DEVELOPMENT_DIRECTORIES) for path in excluded)


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
