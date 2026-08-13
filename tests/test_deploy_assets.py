from __future__ import annotations

import configparser
import os
import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"


def read_asset(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def parse_env_example() -> tuple[dict[str, str], list[str]]:
    values: dict[str, str] = {}
    comments: list[str] = []
    for raw_line in read_asset("deploy/hermes.env.example").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            comments.append(line[1:].strip())
            continue
        key, separator, value = line.partition("=")
        assert separator, f"invalid dotenv line: {raw_line!r}"
        values[key] = value
    return values, comments


def parse_unit() -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    parser.read_string(read_asset("deploy/hermes-gateway.service"))
    return parser


def test_env_template_has_only_required_nonsecret_values() -> None:
    values, comments = parse_env_example()
    assert values == {
        "HERMES_HOME": "/var/lib/hermes/.hermes",
        "XMPP_JID": "hermes@aversa.run/Hermes",
        "XMPP_ALLOWED_USERS": "admin@aversa.run,yuklya@aversa.run,julia@aversa.run",
        "XMPP_NICK": "Hermes",
        "XMPP_STATE_PATH": "/var/lib/hermes/.hermes/xmpp/rooms.json",
    }
    instructions = "\n".join(comments)
    assert "XMPP_PASSWORD" in instructions
    assert "hermes model" in instructions
    assert not any("PASSWORD" in key or "KEY" in key or "TOKEN" in key for key in values)


def test_unit_runs_gateway_as_unprivileged_hermes_user() -> None:
    unit = parse_unit()
    service = unit["Service"]
    assert service["User"] == "hermes"
    assert service["Group"] == "hermes"
    assert service["EnvironmentFile"] == "/etc/hermes/hermes.env"
    assert service["WorkingDirectory"] == "/var/lib/hermes"
    assert re.fullmatch(r"/[A-Za-z0-9_./+-]+ gateway run", service["ExecStart"])
    assert "root" not in service["ExecStart"].lower()


def test_unit_has_required_sandbox_and_restart_policy() -> None:
    unit = parse_unit()
    service = unit["Service"]
    assert service["NoNewPrivileges"] == "true"
    assert service["PrivateTmp"] == "true"
    assert service["ProtectSystem"] == "strict"
    assert service["ReadWritePaths"] == "/var/lib/hermes"
    assert service["UMask"] == "0077"
    assert service["Restart"] == "on-failure"
    assert service["RestartSec"] == "5"
    assert "ProtectHome" not in service


def test_unit_waits_for_docker_without_running_as_root() -> None:
    unit = parse_unit()
    dependencies = " ".join(
        value
        for section in ("Unit", "Service")
        for key, value in unit[section].items()
        if key in {"After", "Requires", "Wants", "SupplementaryGroups"}
    )
    assert "docker" in dependencies.lower()
    assert unit["Service"].get("SupplementaryGroups") == "docker"


def test_deploy_assets_contain_no_literal_secret_assignments() -> None:
    combined = "\n".join(
        read_asset(path)
        for path in (
            "deploy/hermes.env.example",
            "deploy/hermes-gateway.service",
            "deploy/install-on-ubuntu.sh",
            "README.md",
        )
    )
    secret_assignment = re.compile(
        r"(?im)^(?!\s*#)\s*(?:export\s+)?(?:XMPP_PASSWORD|[A-Z0-9_]*(?:API_KEY|TOKEN))\s*=\s*[^\s\"'$]"
    )
    assert not secret_assignment.search(combined)
    assert not re.search(r"(?i)(?:password|api[_ -]?key)\s*[:=]\s*['\"]?[A-Za-z0-9_-]{12,}", combined)


def test_installer_is_syntactically_valid_bash() -> None:
    bash = shutil.which("bash")
    if os.name == "nt":
        git_bash = Path(r"C:\Program Files\Git\bin\bash.exe")
        if git_bash.is_file():
            bash = str(git_bash)
    if not bash:
        import pytest

        pytest.skip("bash is unavailable; run bash -n on Ubuntu before installation")
    result = subprocess.run(
        [bash, "-n", "deploy/install-on-ubuntu.sh"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_installer_fails_closed_before_mutating_unsupported_hosts() -> None:
    script = read_asset("deploy/install-on-ubuntu.sh")
    root_guard = script.index('id -u')
    os_release = script.index('/etc/os-release')
    ubuntu_guard = script.index('ID')
    version_guard = script.index('24.04')
    first_mutation = min(script.index("apt-get"), script.index("useradd"), script.index("install -d"))
    assert max(root_guard, os_release, ubuntu_guard, version_guard) < first_mutation
    assert "set -Eeuo pipefail" in script


def test_installer_uses_official_installer_and_hermes_owned_environment() -> None:
    script = read_asset("deploy/install-on-ubuntu.sh")
    assert "https://hermes-agent.nousresearch.com/install.sh" in script
    assert "--skip-setup" in script
    assert re.search(r"(?:runuser|sudo)\b[^\n]*hermes", script)
    assert re.search(r"HERMES_HOME=[\"']?/var/lib/hermes/\.hermes", script)
    assert re.search(r'UV_BIN=.*(?:command -v|realpath|readlink)', script)
    assert re.search(r'HERMES_BIN=.*(?:command -v|realpath|readlink)', script)
    assert re.search(r'case\s+"\$HERMES_BIN:\$UV_BIN"', script)
    assert "unsafe executable path" in script
    assert re.search(r'"\$UV_BIN"\s+pip\s+install', script)
    assert "slixmpp>=1.12,<2" in script
    assert "pytest" in script


def test_installer_preserves_env_and_stages_verified_unit() -> None:
    script = read_asset("deploy/install-on-ubuntu.sh")
    env_guard = re.search(r'if\s+\[\s+!\s+-e\s+["\']?\$?ENV_FILE', script)
    assert env_guard
    assert re.search(r"install\b[^\n]*-m\s+0600[^\n]*hermes\.env\.example", script)
    verify = script.index("systemd-analyze verify")
    unit_install = script.index('install -o root -g root -m 0644 "$UNIT_TMP" "$UNIT_FILE"')
    assert verify < unit_install
    assert re.search(r"systemctl\s+disable\s+--now\s+hermes-gateway", script)
    assert "systemctl enable" not in script


def test_installer_checks_docker_group_and_keeps_plugin_private() -> None:
    script = read_asset("deploy/install-on-ubuntu.sh")
    assert re.search(r"getent\s+group\s+docker", script)
    assert re.search(r"usermod\s+-aG\s+docker\s+hermes", script)
    assert re.search(r"PLUGIN_(?:SOURCE|SRC)", script)
    assert re.search(r"PLUGIN_(?:DEST|TARGET)", script)
    assert re.search(r"chmod\s+-R\s+[^\n]*o-rwx", script)
    assert not re.search(r"chmod\s+(?:-R\s+)?(?:777|666)\b", script)


def test_installer_validates_identity_and_source_before_replacing_plugin() -> None:
    script = read_asset("deploy/install-on-ubuntu.sh")
    assert re.search(r"groupadd\s+--system\s+hermes", script)
    assert "useradd --system" in script
    assert "--gid hermes" in script
    assert re.search(r"find\s+[\"']?\$PLUGIN_SOURCE[\"']?\s+-type\s+l", script)
    stop = script.index("systemctl disable --now hermes-gateway")
    replace = script.index('rm -rf -- "$PLUGIN_DEST"')
    assert stop < replace


def test_readme_documents_secret_safe_setup_and_operations() -> None:
    readme = read_asset("README.md")
    required = (
        'read -rsp',
        'ejabberdctl register hermes aversa.run "$BOT_PASSWORD"',
        "unset BOT_PASSWORD",
        "sudo -u hermes -H env HERMES_HOME=/var/lib/hermes/.hermes hermes model",
        "sudo -u hermes -H env HERMES_HOME=/var/lib/hermes/.hermes hermes config set terminal.backend docker",
        "sudo -u hermes -H env HERMES_HOME=/var/lib/hermes/.hermes hermes doctor",
        "sudo systemctl enable --now hermes-gateway",
        "sudo systemctl status hermes-gateway --no-pager",
        "sudo journalctl -u hermes-gateway -n 100 --no-pager",
    )
    positions = [readme.index(command) for command in required]
    assert positions == sorted(positions)
    register = readme.index('ejabberdctl register hermes aversa.run "$BOT_PASSWORD"')
    unset = readme.index("unset BOT_PASSWORD")
    assert register < unset
