from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def tracked_text_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    paths = []
    for raw_path in result.stdout.split(b"\0"):
        if not raw_path:
            continue
        path = ROOT / raw_path.decode("utf-8")
        data = path.read_bytes()
        if b"\0" not in data:
            data.decode("utf-8")
            paths.append(path)
    return paths


def private_identifiers() -> tuple[str, ...]:
    domain = "aversa" + ".run"
    provider = "api." + "aitunnel" + ".ru"
    return (
        domain,
        ".".join(("193", "233", "250", "68")),
        ".".join(("109", "107", "189", "155")),
        "admin@" + domain,
        "yuklya@" + domain,
        "julia@" + domain,
        "hermes@" + domain,
        "https://" + provider + "/v1",
    )


def test_tracked_text_files_contain_no_private_identifiers() -> None:
    matches = []
    for path in tracked_text_files():
        text = path.read_text(encoding="utf-8")
        folded = text.casefold()
        if any(private.casefold() in folded for private in private_identifiers()):
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
