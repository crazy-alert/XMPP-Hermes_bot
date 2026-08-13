from __future__ import annotations

import configparser
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def read_asset(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def bash_path(path: Path) -> str:
    value = str(path.resolve())
    if os.name == "nt":
        return f"/{value[0].lower()}{value[2:].replace(chr(92), '/')}"
    return value


def find_bash() -> str:
    found = shutil.which("bash")
    git_bash = Path(r"C:\Program Files\Git\bin\bash.exe")
    if os.name == "nt" and git_bash.is_file():
        found = str(git_bash)
    if not found:
        pytest.skip("bash is unavailable")
    return found


def write_executable(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\nset -eu\n" + body, encoding="utf-8", newline="\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def build_harness(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    fake_root = tmp_path / "root"
    bin_dir = tmp_path / "bin"
    state = tmp_path / "state"
    bin_dir.mkdir()
    state.mkdir()
    (fake_root / "etc").mkdir(parents=True)
    (fake_root / "etc/os-release").write_text('ID=ubuntu\nVERSION_ID="24.04"\n')

    logger = 'printf "%s\\n" "$(basename "$0") $*" >>"$HERMES_TEST_LOG"\n'
    for name in ("apt-get", "groupadd", "useradd", "usermod", "chown"):
        write_executable(bin_dir / name, logger)
    write_executable(bin_dir / "dpkg", "exit 0\n")

    write_executable(
        bin_dir / "getent",
        """
case "$1:$2" in
  group:docker) echo 'docker:x:999:' ;;
  group:hermes) [ -e "$HERMES_TEST_STATE/group" ] && echo 'hermes:x:998:' || exit 2 ;;
  passwd:hermes) [ -e "$HERMES_TEST_STATE/user" ] && echo 'hermes:x:998:998::/var/lib/hermes:/usr/sbin/nologin' || exit 2 ;;
  *) exit 2 ;;
esac
""",
    )
    write_executable(
        bin_dir / "id",
        """
if [ "${HERMES_TEST_IDENTITY_CONFLICT:-0}" = 1 ] && [ "${1:-}:${2:-}" = "-gn:hermes" ]; then echo wrong; exit 0; fi
case "${1:-}" in
  -u) echo 0 ;;
  -gn) echo hermes ;;
  *) exit 1 ;;
esac
""",
    )
    write_executable(
        bin_dir / "runuser",
        logger + """
shift 2
[ "${1:-}" = -- ] && shift
exec "$@"
""",
    )
    write_executable(
        bin_dir / "curl",
        logger
        + """
out=''
while [ "$#" -gt 0 ]; do
  [ "$1" = --output ] && { out="$2"; shift 2; continue; }
  shift
done
cp "$HERMES_TEST_UPSTREAM" "$out"
""",
    )
    write_executable(
        bin_dir / "systemd-analyze",
        logger + '[ "${HERMES_TEST_FAIL_VERIFY:-0}" != 1 ]\n',
    )
    write_executable(
        bin_dir / "systemctl",
        logger
        + """
case "$1" in
  is-active) [ -e "$HERMES_TEST_STATE/active" ] ;;
  is-enabled) [ -e "$HERMES_TEST_STATE/enabled" ] ;;
  stop) [ "${HERMES_TEST_FAIL_STOP:-0}" != 1 ] && rm -f "$HERMES_TEST_STATE/active" ;;
  disable) rm -f "$HERMES_TEST_STATE/enabled" ;;
  start) touch "$HERMES_TEST_STATE/active" ;;
  enable) touch "$HERMES_TEST_STATE/enabled" ;;
  daemon-reload) : ;;
  *) exit 2 ;;
esac
""",
    )

    upstream = tmp_path / "upstream.sh"
    write_executable(
        upstream,
        """
install_dir=''
while [ "$#" -gt 0 ]; do
  [ "$1" = --dir ] && { install_dir="$2"; shift 2; continue; }
  shift
done
mkdir -p "$install_dir/venv/bin" "$HERMES_HOME/bin"
printf '#!/bin/sh\\nexit 0\\n' >"$install_dir/venv/bin/hermes"
printf '#!/bin/sh\\nexit 0\\n' >"$install_dir/venv/bin/python"
printf '#!/bin/sh\\nexit 0\\n' >"$HERMES_HOME/bin/uv"
chmod 700 "$HERMES_HOME/bin/uv" "$install_dir/venv/bin/hermes" "$install_dir/venv/bin/python"
""",
    )
    env = os.environ.copy()
    env.update(
        HERMES_TEST_STATE=bash_path(state),
        HERMES_TEST_LOG=bash_path(tmp_path / "commands.log"),
        HERMES_TEST_UPSTREAM=bash_path(upstream),
        PATH=bash_path(bin_dir) + ":" + env["PATH"],
    )
    return fake_root, env


def generated_installer(tmp_path: Path, fake_root: Path, *, inject_after: str | None = None) -> Path:
    source = read_asset("deploy/install-on-ubuntu.sh")
    deploy = bash_path(ROOT / "deploy")
    repo = bash_path(ROOT)
    prefix = bash_path(fake_root)
    bin_dir = bash_path(tmp_path / "bin")
    replacements = {
        'ROOT_PREFIX=""': f'ROOT_PREFIX={prefix!r}\nPATH={bin_dir!r}:$PATH\nexport PATH',
        'SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)': f"SCRIPT_DIR={deploy!r}",
        'REPO_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd -P)': f"REPO_DIR={repo!r}",
        '    install -d -o "$2" -g "$3" -m "$4" "$1"': '    mkdir -p -- "$1"\n    chmod "$4" "$1"',
        '    if [ ! -d "$directory" ] || [ -L "$directory" ] || [ "$(stat -c %U "$directory")" != hermes ]; then': '    if [ ! -d "$directory" ] || [ -L "$directory" ]; then',
        '    [ -z "$(find /var/lib/hermes -xdev ! -user hermes -print -quit)" ] || return 1': '    : # ownership is asserted through the chown stub in this generated copy',
        'in /var/lib/hermes/*) : ;;': f'in {prefix}/var/lib/hermes/*) : ;;',
        '    printf \'%s  %s\\n\' "$INSTALLER_SHA256" "$INSTALLER_TMP" | sha256sum --check --status': '    : # fake upstream fixture; production digest check is unchanged',
    }
    for old, new in replacements.items():
        assert old in source, old
        source = source.replace(old, new, 1)
    if inject_after:
        assert inject_after in source
        sentinel = bash_path(tmp_path / "injection-reached")
        source = source.replace(inject_after, inject_after + f"\ntouch {sentinel!r}\nfalse # generated test-only fault", 1)
    generated = tmp_path / ("install-fault.sh" if inject_after else "install-under-test.sh")
    generated.write_text(source, encoding="utf-8", newline="\n")
    return generated


def run_installer(tmp_path: Path, *, extra: dict[str, str] | None = None, script: Path | None = None) -> tuple[subprocess.CompletedProcess[str], Path, dict[str, str]]:
    fake_root, env = build_harness(tmp_path)
    if extra:
        env.update(extra)
    script = script or generated_installer(tmp_path, fake_root)
    env["TEST_GENERATED_INSTALLER"] = str(script)
    result = subprocess.run(
        [find_bash(), str(script)], cwd=ROOT, env=env,
        text=True, capture_output=True, check=False,
    )
    return result, fake_root, env


def parse_unit() -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    parser.read_string(read_asset("deploy/hermes-gateway.service"))
    return parser


def test_env_template_and_unit_contract() -> None:
    values = {}
    comments = []
    for raw in read_asset("deploy/hermes.env.example").splitlines():
        if raw.startswith("#"):
            comments.append(raw)
        elif raw:
            key, value = raw.split("=", 1)
            values[key] = value
    assert values == {
        "HERMES_HOME": "/var/lib/hermes/.hermes",
        "XMPP_JID": "bot@example.com/Hermes",
        "XMPP_ALLOWED_USERS": "admin@example.com",
        "XMPP_NICK": "Hermes",
        "XMPP_STATE_PATH": "/var/lib/hermes/.hermes/xmpp/rooms.json",
    }
    assert "XMPP_PASSWORD" in "\n".join(comments)
    service = parse_unit()["Service"]
    assert service["User"] == service["Group"] == "hermes"
    assert service["EnvironmentFile"] == "/etc/hermes/hermes.env"
    assert service["NoNewPrivileges"] == "true"
    assert service["PrivateTmp"] == "true"
    assert service["ProtectSystem"] == "strict"
    assert service["ReadWritePaths"] == "/var/lib/hermes"
    assert service["UMask"] == "0077"
    assert service["Restart"] == "on-failure"
    assert service["RestartSec"] == "5"
    assert service["SupplementaryGroups"] == "docker"


def test_shipped_installer_has_no_test_mode_or_fault_hooks() -> None:
    script = read_asset("deploy/install-on-ubuntu.sh")
    assert "HERMES_INSTALL_TEST" not in script
    assert "HERMES_TEST_FAIL" not in script


def test_official_installer_digest_matches_audited_pinned_commit() -> None:
    script = read_asset("deploy/install-on-ubuntu.sh")
    assert "HERMES_COMMIT=3c27eb6234bf91b8ceee9e9071591b31e9b148cb" in script
    assert "INSTALLER_SHA256=45f589461248c7a6ec3aecd7522a69dd49c5c8dbf4798ba1296af5c0c5e7ccd3" in script


def test_runtime_paths_match_pinned_installer_contract() -> None:
    script = read_asset("deploy/install-on-ubuntu.sh")
    assert "UV_BIN=$HERMES_HOME/bin/uv" in script
    assert 'secure_hermes_dir "$HERMES_LOCAL_DISK"' in script
    assert 'chown --no-dereference hermes:hermes "$directory"' in script


@pytest.mark.parametrize(
    ("marker", "old_asset"),
    [
        ('PLUGIN_BACKED_UP=1; fi', "plugin"),
        ('UNIT_BACKED_UP=1; fi', "unit"),
    ],
)
def test_failure_after_backup_rename_restores_old_asset_and_service_state(tmp_path: Path, marker: str, old_asset: str) -> None:
    installed, root, env = run_installer(tmp_path)
    assert installed.returncode == 0, installed.stderr
    plugin = root / "var/lib/hermes/.hermes/plugins/xmpp-platform"
    (plugin / "marker").write_text("old")
    unit = root / "etc/systemd/system/hermes-gateway.service"
    old_unit = unit.read_text()
    state = Path(env["HERMES_TEST_STATE"].replace("/c/", "C:/") if os.name == "nt" else env["HERMES_TEST_STATE"])
    (state / "active").touch()
    (state / "enabled").touch()
    generated = generated_installer(tmp_path, root, inject_after=marker)
    failed = subprocess.run([find_bash(), str(generated)], cwd=ROOT, env=env, text=True, capture_output=True)
    assert failed.returncode != 0
    assert (tmp_path / "injection-reached").exists()
    assert (plugin / "marker").read_text() == "old", old_asset
    assert unit.read_text() == old_unit
    assert (state / "active").exists() and (state / "enabled").exists()


def test_first_install_accepts_official_wrapper_and_uses_managed_venv(tmp_path: Path) -> None:
    result, root, env = run_installer(tmp_path)
    assert result.returncode == 0, result.stderr
    unit = (root / "etc/systemd/system/hermes-gateway.service").read_text()
    assert "ExecStart=/var/lib/hermes/.hermes/hermes-agent/venv/bin/hermes gateway run" in unit
    log = Path(env["HERMES_TEST_LOG"].replace("/c/", "C:/") if os.name == "nt" else env["HERMES_TEST_LOG"]).read_text()
    assert "usermod -aG docker hermes" in log
    assert "--commit 3c27eb6234bf91b8ceee9e9071591b31e9b148cb" in log
    assert log.index("curl ") < log.index("usermod ")


def test_rerun_skips_runtime_installer_and_preserves_env(tmp_path: Path) -> None:
    first, root, env = run_installer(tmp_path)
    assert first.returncode == 0, first.stderr
    env_file = root / "etc/hermes/hermes.env"
    env_file.write_text("XMPP_PASSWORD=kept\n")
    Path(env["HERMES_TEST_LOG"].replace("/c/", "C:/") if os.name == "nt" else env["HERMES_TEST_LOG"]).write_text("")
    second = subprocess.run([find_bash(), env["TEST_GENERATED_INSTALLER"]], cwd=ROOT, env=env, text=True, capture_output=True)
    assert second.returncode == 0, second.stderr
    assert env_file.read_text() == "XMPP_PASSWORD=kept\n"
    log = Path(env["HERMES_TEST_LOG"].replace("/c/", "C:/") if os.name == "nt" else env["HERMES_TEST_LOG"]).read_text()
    assert not any(line.startswith("curl ") for line in log.splitlines())


@pytest.mark.parametrize("failure", ["HERMES_TEST_FAIL_VERIFY", "HERMES_TEST_FAIL_STOP"])
def test_failed_verify_or_stop_keeps_old_deployment_and_service_state(tmp_path: Path, failure: str) -> None:
    result, root, env = run_installer(tmp_path)
    assert result.returncode == 0, result.stderr
    plugin = root / "var/lib/hermes/.hermes/plugins/xmpp-platform"
    (plugin / "marker").write_text("old")
    unit = root / "etc/systemd/system/hermes-gateway.service"
    old_unit = unit.read_text()
    state = Path(env["HERMES_TEST_STATE"].replace("/c/", "C:/") if os.name == "nt" else env["HERMES_TEST_STATE"])
    (state / "active").touch()
    (state / "enabled").touch()
    env[failure] = "1"
    failed = subprocess.run([find_bash(), env["TEST_GENERATED_INSTALLER"]], cwd=ROOT, env=env, text=True, capture_output=True)
    assert failed.returncode != 0
    assert (plugin / "marker").read_text() == "old"
    assert unit.read_text() == old_unit
    assert (state / "active").exists() and (state / "enabled").exists()


def test_plugin_copy_is_explicit_allowlist(tmp_path: Path) -> None:
    source = ROOT / "hermes-xmpp"
    junk = source / "__pycache__"
    junk.mkdir(exist_ok=True)
    (junk / "secret.env").write_text("no")
    try:
        result, root, _ = run_installer(tmp_path)
        assert result.returncode == 0, result.stderr
        dest = root / "var/lib/hermes/.hermes/plugins/xmpp-platform"
        assert sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file()) == [
            "adapter.py", "plugin.yaml", "xmpp_bridge/__init__.py", "xmpp_bridge/client.py",
            "xmpp_bridge/models.py", "xmpp_bridge/policy.py", "xmpp_bridge/state.py",
        ]
    finally:
        shutil.rmtree(junk)


def test_identity_conflict_fails_before_apt(tmp_path: Path) -> None:
    fake_root, env = build_harness(tmp_path)
    state = Path(env["HERMES_TEST_STATE"].replace("/c/", "C:/") if os.name == "nt" else env["HERMES_TEST_STATE"])
    (state / "group").touch()
    (state / "user").touch()
    env["HERMES_TEST_IDENTITY_CONFLICT"] = "1"
    generated = generated_installer(tmp_path, fake_root)
    result = subprocess.run([find_bash(), str(generated)], cwd=ROOT, env=env, text=True, capture_output=True)
    assert result.returncode != 0
    log_path = tmp_path / "commands.log"
    assert not log_path.exists() or "apt-get" not in log_path.read_text()


@pytest.mark.parametrize("unsafe_kind", ["symlink", "file"])
def test_unsafe_existing_local_path_fails_before_mutation(tmp_path: Path, unsafe_kind: str) -> None:
    fake_root, env = build_harness(tmp_path)
    hermes_home = fake_root / "var/lib/hermes"
    hermes_home.mkdir(parents=True)
    local_path = hermes_home / ".local"
    external = tmp_path / "external"
    external.mkdir()
    marker = external / "marker"
    marker.write_text("unchanged")
    before_mode = stat.S_IMODE(external.stat().st_mode)
    if unsafe_kind == "symlink":
        try:
            local_path.symlink_to(external, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"directory symlinks are unavailable: {exc}")
    else:
        local_path.write_text("not a directory")

    generated = generated_installer(tmp_path, fake_root)
    result = subprocess.run([find_bash(), str(generated)], cwd=ROOT, env=env, text=True, capture_output=True)

    assert result.returncode != 0
    assert marker.read_text() == "unchanged"
    assert stat.S_IMODE(external.stat().st_mode) == before_mode
    log_path = tmp_path / "commands.log"
    assert not log_path.exists() or "apt-get" not in log_path.read_text()
    assert not (fake_root / "etc/systemd/system/hermes-gateway.service").exists()


@pytest.mark.parametrize("unsafe_kind", ["symlink", "dangling_symlink", "file"])
def test_unsafe_existing_hermes_home_fails_before_mutation(tmp_path: Path, unsafe_kind: str) -> None:
    fake_root, env = build_harness(tmp_path)
    account_home = fake_root / "var/lib/hermes"
    account_home.mkdir(parents=True)
    hermes_home = account_home / ".hermes"
    external = tmp_path / "external-hermes"
    external.mkdir()
    marker = external / "marker"
    marker.write_text("unchanged")
    before_mode = stat.S_IMODE(external.stat().st_mode)
    try:
        if unsafe_kind == "symlink":
            hermes_home.symlink_to(external, target_is_directory=True)
        elif unsafe_kind == "dangling_symlink":
            hermes_home.symlink_to(tmp_path / "missing-target", target_is_directory=True)
        else:
            hermes_home.write_text("not a directory")
    except OSError as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")

    generated = generated_installer(tmp_path, fake_root)
    result = subprocess.run([find_bash(), str(generated)], cwd=ROOT, env=env, text=True, capture_output=True)

    assert result.returncode != 0
    assert marker.read_text() == "unchanged"
    assert stat.S_IMODE(external.stat().st_mode) == before_mode
    log_path = tmp_path / "commands.log"
    assert not log_path.exists() or "apt-get" not in log_path.read_text()
    assert not (fake_root / "etc/hermes/hermes.env").exists()
    assert not (fake_root / "etc/systemd/system/hermes-gateway.service").exists()
    assert not (hermes_home / "plugins/xmpp-platform").exists()


@pytest.mark.parametrize("unsafe_kind", ["symlink", "dangling_symlink", "file"])
def test_unsafe_existing_plugins_path_fails_before_mutation(tmp_path: Path, unsafe_kind: str) -> None:
    fake_root, env = build_harness(tmp_path)
    hermes_home = fake_root / "var/lib/hermes/.hermes"
    hermes_home.mkdir(parents=True)
    plugins = hermes_home / "plugins"
    external = tmp_path / "external-plugins"
    external.mkdir()
    marker = external / "marker"
    marker.write_text("unchanged")
    before_mode = stat.S_IMODE(external.stat().st_mode)
    try:
        if unsafe_kind == "symlink":
            plugins.symlink_to(external, target_is_directory=True)
        elif unsafe_kind == "dangling_symlink":
            plugins.symlink_to(tmp_path / "missing-plugins", target_is_directory=True)
        else:
            plugins.write_text("not a directory")
    except OSError as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")

    generated = generated_installer(tmp_path, fake_root)
    result = subprocess.run([find_bash(), str(generated)], cwd=ROOT, env=env, text=True, capture_output=True)

    assert result.returncode != 0
    assert marker.read_text() == "unchanged"
    assert stat.S_IMODE(external.stat().st_mode) == before_mode
    log_path = tmp_path / "commands.log"
    assert not log_path.exists() or "apt-get" not in log_path.read_text()
    assert not (fake_root / "etc/hermes/hermes.env").exists()
    assert not (fake_root / "etc/systemd/system/hermes-gateway.service").exists()
    assert not (plugins / "xmpp-platform").exists()


@pytest.mark.parametrize(
    "relative",
    [
        "hermes-agent",
        "hermes-agent/venv",
        "hermes-agent/venv/bin",
        "bin",
    ],
)
@pytest.mark.parametrize("unsafe_kind", ["symlink", "dangling_symlink", "file"])
def test_unsafe_runtime_directory_fails_before_apt(tmp_path: Path, relative: str, unsafe_kind: str) -> None:
    fake_root, env = build_harness(tmp_path)
    hermes_home = fake_root / "var/lib/hermes/.hermes"
    target = hermes_home / relative
    target.parent.mkdir(parents=True)
    external = tmp_path / "external-runtime"
    external.mkdir()
    marker = external / "marker"
    marker.write_text("unchanged")
    before_mode = stat.S_IMODE(external.stat().st_mode)
    try:
        if unsafe_kind == "symlink":
            target.symlink_to(external, target_is_directory=True)
        elif unsafe_kind == "dangling_symlink":
            target.symlink_to(tmp_path / "missing-runtime", target_is_directory=True)
        else:
            target.write_text("not a directory")
    except OSError as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")

    generated = generated_installer(tmp_path, fake_root)
    result = subprocess.run([find_bash(), str(generated)], cwd=ROOT, env=env, text=True, capture_output=True)

    assert result.returncode != 0
    assert marker.read_text() == "unchanged"
    assert stat.S_IMODE(external.stat().st_mode) == before_mode
    log_path = tmp_path / "commands.log"
    assert not log_path.exists() or "apt-get" not in log_path.read_text()
    assert not log_path.exists() or "curl" not in log_path.read_text()


@pytest.mark.parametrize(
    "relative",
    [
        "hermes-agent/venv/bin/hermes",
        "hermes-agent/venv/bin/python",
        "bin/uv",
    ],
)
@pytest.mark.parametrize("unsafe_kind", ["symlink", "directory"])
def test_unsafe_runtime_executable_never_runs_as_root(tmp_path: Path, relative: str, unsafe_kind: str) -> None:
    fake_root, env = build_harness(tmp_path)
    target = fake_root / "var/lib/hermes/.hermes" / relative
    target.parent.mkdir(parents=True)
    executed = tmp_path / "executed"
    attacker = tmp_path / "attacker"
    write_executable(attacker, f"touch {bash_path(executed)!r}\n")
    try:
        if unsafe_kind == "symlink":
            target.symlink_to(attacker)
        else:
            target.mkdir()
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")

    generated = generated_installer(tmp_path, fake_root)
    result = subprocess.run([find_bash(), str(generated)], cwd=ROOT, env=env, text=True, capture_output=True)

    assert result.returncode != 0
    assert not executed.exists()
    log_path = tmp_path / "commands.log"
    assert not log_path.exists() or "apt-get" not in log_path.read_text()
    assert not log_path.exists() or "curl" not in log_path.read_text()


def test_env_and_installed_tree_ownership_and_modes_are_enforced(tmp_path: Path) -> None:
    result, root, env = run_installer(tmp_path)
    assert result.returncode == 0, result.stderr
    env_file = root / "etc/hermes/hermes.env"
    if os.name != "nt":
        assert stat.S_IMODE(env_file.stat().st_mode) & 0o077 == 0
    log = Path(env["HERMES_TEST_LOG"].replace("/c/", "C:/") if os.name == "nt" else env["HERMES_TEST_LOG"]).read_text()
    assert "chown root:root " in log
    assert "chown -R hermes:hermes " in log


def test_bash_syntax() -> None:
    result = subprocess.run([find_bash(), "-n", "deploy/install-on-ubuntu.sh"], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_unix_deployment_assets_are_forced_to_lf_in_archives() -> None:
    attributes = read_asset(".gitattributes").splitlines()
    assert "deploy/*.sh text eol=lf" in attributes
    assert "deploy/*.service text eol=lf" in attributes
    assert "deploy/*.example text eol=lf" in attributes
