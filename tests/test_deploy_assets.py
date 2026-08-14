from __future__ import annotations

import configparser
import json
import os
import re
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


def bash_lexical_path(path: Path) -> str:
    value = str(path.absolute())
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
    for name in ("groupadd", "useradd", "usermod", "chown"):
        write_executable(bin_dir / name, logger)
    write_executable(
        bin_dir / "apt-get",
        logger
        + """
if [ "${HERMES_TEST_FAIL_DOCKER_INSTALL:-0}" = 1 ] && [ "${1:-}" = install ]; then exit 1; fi
case " $* " in *' docker.io '*) touch "$HERMES_TEST_STATE/docker-package" ;; esac
""",
    )
    write_executable(bin_dir / "dpkg", "exit 0\n")

    write_executable(
        bin_dir / "getent",
        """
case "$1:$2" in
  group:docker) [ "${HERMES_TEST_DOCKER_READY:-1}" = 1 ] || [ -e "$HERMES_TEST_STATE/docker-package" ] || exit 2; echo 'docker:x:999:' ;;
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
  enable)
    if [ "${2:-}" = --now ] && [ "${3:-}" = docker ]; then touch "$HERMES_TEST_STATE/docker-daemon"; exit 0; fi
    [ "${3:-}" != hermes-reboot-helper.path ] || exit 0
    [ "${HERMES_TEST_FAIL_ENABLE_NOW:-0}" != 1 ] || exit 1
    touch "$HERMES_TEST_STATE/enabled"
    [ "${2:-}" != --now ] || [ "${HERMES_TEST_FALSE_ENABLE:-0}" = 1 ] || touch "$HERMES_TEST_STATE/active"
    ;;
  is-active) [ "${HERMES_TEST_FALSE_ACTIVE:-0}" != 1 ] && [ -e "$HERMES_TEST_STATE/active" ] ;;
  is-enabled) [ "${HERMES_TEST_FALSE_ENABLE:-0}" != 1 ] && [ -e "$HERMES_TEST_STATE/enabled" ] ;;
  stop) [ "${HERMES_TEST_FAIL_STOP:-0}" != 1 ] && rm -f "$HERMES_TEST_STATE/active" ;;
  disable) rm -f "$HERMES_TEST_STATE/enabled" ;;
  start) touch "$HERMES_TEST_STATE/active" ;;
  cat) [ "${HERMES_TEST_MISSING_UNIT:-0}" != 1 ] ;;
  status|journalctl) : ;;
  daemon-reload) : ;;
  *) exit 2 ;;
esac
""",
    )
    write_executable(
        bin_dir / "docker",
        """
if [ "${1:-}" = info ] && [ "${HERMES_TEST_DOCKER_READY:-1}" = 1 -o \\( -e "$HERMES_TEST_STATE/docker-package" -a -e "$HERMES_TEST_STATE/docker-daemon" -a "${HERMES_TEST_FAIL_DOCKER_DAEMON:-0}" != 1 \\) ]; then exit 0; fi
exit 1
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
printf '#!/bin/sh\\n[ "$1" != doctor ] || [ "${HERMES_TEST_FAIL_DOCTOR:-0}" != 1 ]\\n' >"$install_dir/venv/bin/hermes"
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


def generated_installer(
    tmp_path: Path,
    fake_root: Path,
    *,
    inject_after: str | None = None,
    race_after: str | None = None,
    interactive_stdin: bool = False,
) -> Path:
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
        '    install -o root -g root -m "$mode" -- "$source" "$destination"': '    cp -- "$source" "$destination"\n    chmod "$mode" "$destination"',
        '    if [ ! -d "$directory" ] || [ -L "$directory" ] || [ "$(stat -c %U "$directory")" != hermes ]; then': '    if [ ! -d "$directory" ] || [ -L "$directory" ]; then',
        '        test -z "$(find "$4" -xdev ! -user hermes -print -quit)" || exit 1': '        : # ownership is asserted through the chown stub in this generated copy',
        'if [ -L "$HERMES_ACCOUNT_HOME_DISK" ] || [ "$(stat -c %U:%G:%a "$HERMES_ACCOUNT_HOME_DISK")" != root:root:755 ]; then': 'if [ -L "$HERMES_ACCOUNT_HOME_DISK" ]; then',
        'if [ "$(stat -c %U:%G:%a "$HERMES_ACCOUNT_HOME_DISK")" != root:root:755 ]; then': 'if [ -L "$HERMES_ACCOUNT_HOME_DISK" ]; then',
        'if [ -L "$REBOOT_SPOOL_DIR" ] || [ "$(stat -c %U:%G:%a "$REBOOT_SPOOL_DIR")" != root:hermes:730 ]; then': 'if [ -L "$REBOOT_SPOOL_DIR" ]; then',
        'if [ -L "$REBOOT_CONTROL_DIR" ] || [ "$(stat -c %U:%G:%a "$REBOOT_CONTROL_DIR")" != root:root:700 ]; then': 'if [ -L "$REBOOT_CONTROL_DIR" ]; then',
        '        [ "$(stat -c %U:%G "$destination")" = root:root ] || return 1': '        : # ownership is asserted by the production installer',
        '    [ -f "$destination" ] && [ ! -L "$destination" ] && [ "$(stat -c %U:%G:%a "$destination")" = "root:root:$mode" ]': '    [ -f "$destination" ] && [ ! -L "$destination" ]',
        'in /var/lib/hermes/*) : ;;': f'in {prefix}/var/lib/hermes/*) : ;;',
        '    printf \'%s  %s\\n\' "$INSTALLER_SHA256" "$INSTALLER_TMP" | sha256sum --check --status': '    : # fake upstream fixture; production digest check is unchanged',
    }
    for old, new in replacements.items():
        assert old in source, old
        source = source.replace(old, new, 1)
    if interactive_stdin:
        source = source.replace('if [ ! -t 0 ]; then', 'if false; then')
    if inject_after:
        assert inject_after in source
        sentinel = bash_path(tmp_path / "injection-reached")
        source = source.replace(inject_after, inject_after + f"\ntouch {sentinel!r}\nfalse # generated test-only fault", 1)
    if race_after:
        assert race_after in source
        account_home = bash_path(fake_root / "var/lib/hermes")
        race_link = bash_lexical_path(tmp_path / "race-link")
        race = f"\nrm -rf -- {account_home!r}\nmv -- {race_link!r} {account_home!r} # generated test-only race"
        source = source.replace(race_after, race_after + race, 1)
    generated = tmp_path / ("install-fault.sh" if inject_after else "install-under-test.sh")
    generated.write_text(source, encoding="utf-8", newline="\n")
    return generated


def run_installer(
    tmp_path: Path,
    *,
    extra: dict[str, str] | None = None,
    script: Path | None = None,
    input_text: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path, dict[str, str]]:
    fake_root, env = build_harness(tmp_path)
    if extra:
        env.update(extra)
    script = script or generated_installer(tmp_path, fake_root)
    env["TEST_GENERATED_INSTALLER"] = str(script)
    result = subprocess.run(
        [find_bash(), str(script)], cwd=ROOT, env=env,
        text=True, input=input_text, capture_output=True, check=False,
    )
    return result, fake_root, env


def generated_bootstrap(
    tmp_path: Path,
    *,
    docker_missing: bool = False,
    fail_state_commit: bool = False,
    fail_env_backup: bool = False,
    recover_only: bool = False,
    interrupt_after_commit_marker: str | None = None,
    pause_lock: bool = False,
) -> tuple[Path, Path, dict[str, str]]:
    """Generated bootstrap harness; production installer contains no test hooks."""
    fake_root = tmp_path / "bootstrap-root"
    (fake_root / "etc/hermes").mkdir(parents=True)
    (fake_root / "var/lib/hermes/.hermes/xmpp").mkdir(parents=True)
    os_release = tmp_path / "os-release"
    os_release.write_text('ID=ubuntu\nVERSION_ID="24.04"\n', encoding="utf-8")
    log = tmp_path / "bootstrap.log"
    source = read_asset("installer.sh")
    prefix = bash_path(fake_root)
    inner = f'''\
printf '%s\\n' inner >>{bash_path(log)!r}
mkdir -p {bash_path(fake_root / 'etc/hermes')!r} {bash_path(fake_root / 'var/lib/hermes/.hermes/xmpp')!r}
'''
    prelude = '''\
id() { [ "${1:-}" = -u ] && { printf '0\\n'; return; }; command id "$@"; }
apt-get() { :; }
dpkg() { return 0; }
docker() { [ "${1:-}" = info ]; }
getent() { [ "${1:-}:${2:-}" = group:docker ]; }
git() { case "$*" in *'rev-parse'*) printf '%s\\n' "$TEST_REF";; esac; }
systemctl() { printf 'systemctl %s\\n' "$*" >>"$TEST_LOG"; }
runuser() { shift 3; "$@"; }
chown() { :; }
flock() { :; }
'''
    source = prelude + source
    source = source.replace('. /etc/os-release', '. "$TEST_OS_RELEASE"')
    source = source.replace('apt-get update\napt-get install -y --no-install-recommends ca-certificates git', ':')
    source = source.replace('    apt-get update\n    apt-get install -y --no-install-recommends docker.io\n    systemctl enable --now docker', '    printf \'docker-install\\n\' >>"$TEST_LOG"')
    source = source.replace('STAGE=$(mktemp -d /tmp/hermes-xmpp.XXXXXX)', 'STAGE=$TEST_STAGE')
    for line in (
        'git init -q "$STAGE"',
        'git -C "$STAGE" remote add origin "$REPOSITORY"',
        'git -C "$STAGE" fetch --depth=1 origin "$REF"',
        'git -C "$STAGE" checkout -q --detach FETCH_HEAD',
    ):
        source = source.replace(line, ':')
    source = source.replace('[ -f "$STAGE/deploy/install-on-ubuntu.sh" ] || fail \'verified checkout has no deployment installer\'', ':')
    source = source.replace('HERMES_DEFER_SERVICE_START=1 bash "$STAGE/deploy/install-on-ubuntu.sh"', inner)
    source = source.replace('ENV_DIR=/etc/hermes', f'ENV_DIR={bash_path(fake_root / "etc/hermes")!r}')
    source = source.replace('STATE_DIR=/var/lib/hermes/.hermes/xmpp', f'STATE_DIR={bash_path(fake_root / "var/lib/hermes/.hermes/xmpp")!r}')
    if fail_state_commit:
        target = 'runuser -u hermes -- mv -f "$STATE_TMP" "$STATE_FILE"'
        assert target in source
        source = source.replace(target, 'false # generated commit failure', 1)
    if fail_env_backup:
        target = '    cp -p -- "$ENV_FILE" "$TXN_DIR/env.old"'
        assert target in source
        source = source.replace(target, '    false # generated backup failure', 1)
    if recover_only:
        target = 'mkdir -- "$TXN_DIR"'
        assert target in source
        source = source.replace(target, 'exit 0 # generated recovery-only run\n' + target, 1)
    if interrupt_after_commit_marker:
        target = 'sync_dir "$TXN_DIR"\n\nprintf'
        assert target in source
        source = source.replace(
            target,
            'sync_dir "$TXN_DIR"\n'
            f'kill -{interrupt_after_commit_marker} $$ # generated interruption after durable marker\n'
            '\nprintf',
            1,
        )
    script = tmp_path / "bootstrap-under-test.sh"
    script.write_text(source, encoding="utf-8", newline="\n")
    env = os.environ.copy()
    env.update(TEST_STAGE=bash_path(tmp_path / "stage"), TEST_REF="a" * 40, TEST_LOG=bash_path(log), TEST_OS_RELEASE=bash_path(os_release))
    if docker_missing:
        source = script.read_text(encoding="utf-8").replace(
            '[ -t 0 ] || fail \'Docker Engine is unavailable and installation needs interactive confirmation\'',
            ':',
        ).replace(
            'docker() { [ "${1:-}" = info ]; }',
            'docker() { [ "${TEST_DOCKER_INSTALLED:-0}" = 1 ]; }',
        ).replace(
            'getent() { [ "${1:-}:${2:-}" = group:docker ]; }',
            'getent() { [ "${TEST_DOCKER_INSTALLED:-0}" = 1 ]; }',
        ).replace(
            "printf 'docker-install\\n' >>\"$TEST_LOG\"",
            "printf 'docker-install\\n' >>\"$TEST_LOG\"; export TEST_DOCKER_INSTALLED=1",
        )
        script.write_text(source, encoding="utf-8", newline="\n")
    if pause_lock:
        source = script.read_text(encoding="utf-8").replace(
            'flock() { :; }',
            'flock() { while ! mkdir "$TEST_CONCURRENCY_LOCK" 2>/dev/null; do sleep 0.05; done; }',
        ).replace(
            'flock -x 9\nchmod 0600 "$ENV_DIR/.xmpp-config.lock"',
            'flock -x 9\nchmod 0600 "$ENV_DIR/.xmpp-config.lock"\ntouch "$TEST_LOCK_READY"\nwhile [ ! -e "$TEST_LOCK_RELEASE" ]; do sleep 0.05; done',
        ).replace(
            'sync_dir "$TXN_DIR"\n\nprintf',
            'sync_dir "$TXN_DIR"\nrmdir "$TEST_CONCURRENCY_LOCK" || true\n\nprintf',
        )
        script.write_text(source, encoding="utf-8", newline="\n")
        env.update(
            TEST_CONCURRENCY_LOCK=bash_path(tmp_path / "concurrency.lock"),
            TEST_LOCK_READY=bash_path(tmp_path / "lock.ready"),
            TEST_LOCK_RELEASE=bash_path(tmp_path / "lock.release"),
        )
    return script, fake_root, env


def bootstrap_input(*, owner: str = "owner@example.com", start: str = "n") -> str:
    return "xmpp.example.com\n5223\ndirect\nbot@example.com/Hermes\nHermes\nsecret\n" + owner + "\n" + start + "\n"


def parse_unit() -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    parser.read_string(read_asset("deploy/hermes-gateway.service"))
    return parser


def parse_reboot_unit(relative: str) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    parser.read_string(read_asset(relative))
    return parser


def generated_reboot_helper(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """Exercise the helper against an isolated filesystem without test hooks in production."""
    if os.name == "nt":
        pytest.skip("reboot helper harness requires POSIX ownership and openat semantics")
    spool = tmp_path / "spool"
    control = tmp_path / "control"
    bin_dir = tmp_path / "bin"
    rebooted = tmp_path / "rebooted"
    spool.mkdir()
    control.mkdir()
    bin_dir.mkdir()
    write_executable(bin_dir / "systemctl", f'[ "${{1:-}}" = reboot ] && touch {bash_path(rebooted)!r}\n')
    source = read_asset("deploy/hermes-reboot-helper.sh")
    replacements = {
        "SPOOL_DIR=/var/lib/hermes/reboot-spool": f"SPOOL_DIR={bash_path(spool)!r}",
        "CONTROL_DIR=/var/lib/hermes/reboot-control": f"CONTROL_DIR={bash_path(control)!r}",
        "assert_root_owned_directory() {": "assert_root_owned_directory() { return 0; }\n\n# test replacement\nunused_assert_root_owned_directory() {",
        "assert_spool_directory() {": "assert_spool_directory() { return 0; }\n\n# test replacement\nunused_assert_spool_directory() {",
        'if [ "$(id -u)" -ne 0 ]; then': "if false; then",
        "/usr/bin/python3 -": f"{bash_path(Path(os.sys.executable))!r} -",
        "import grp\n": "",
        "import pwd\n": "",
        "/usr/bin/systemctl reboot": f"{bash_path(bin_dir / 'systemctl')!r} reboot",
        "/usr/bin/sleep 5": ": # no delay in the isolated harness",
        "expected_uid = pwd.getpwnam(\"hermes\").pw_uid": "expected_uid = os.fstat(fd).st_uid",
        "expected_gid = grp.getgrnam(\"hermes\").gr_gid": "expected_gid = os.fstat(fd).st_gid",
        "or stat.S_IMODE(metadata.st_mode) != 0o600\n": "",
        'getattr(os, "O_NOFOLLOW", 0)': "0",
        'control_fd = os.open(control_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))': "control_fd = None",
        "            dir_fd=control_fd,\n": "",
        "        os.close(control_fd)": "        pass",
    }
    for old, new in replacements.items():
        assert old in source, old
        source = source.replace(old, new, 1)
    script = tmp_path / "reboot-helper-under-test.sh"
    script.write_text(source, encoding="utf-8", newline="\n")
    return script, spool, control, rebooted


def test_reboot_helper_units_confine_the_root_action_to_a_typed_spool() -> None:
    service = parse_reboot_unit("deploy/hermes-reboot-helper.service")["Service"]
    path = parse_reboot_unit("deploy/hermes-reboot-helper.path")["Path"]

    assert service["User"] == service["Group"] == "root"
    assert service["ExecStart"] == "/usr/local/libexec/hermes-reboot-helper"
    assert service["NoNewPrivileges"] == "true"
    assert service["ProtectSystem"] == "strict"
    assert service["PrivateTmp"] == "true"
    assert service["ReadWritePaths"] == "/var/lib/hermes/reboot-spool /var/lib/hermes/reboot-control"
    assert path["PathExists"] == "/var/lib/hermes/reboot-spool/request.json"
    assert path["Unit"] == "hermes-reboot-helper.service"
    assert parse_reboot_unit("deploy/hermes-reboot-helper.path")["Install"]["WantedBy"] == "multi-user.target"


def test_reboot_helper_consumes_only_canonical_request_and_never_uses_input_as_argv(tmp_path: Path) -> None:
    helper, spool, control, rebooted = generated_reboot_helper(tmp_path)
    request = spool / "request.json"
    nonce = "0123456789abcdef0123456789abcdef"
    request.write_text(json.dumps({"action": "reboot", "nonce": nonce, "version": 1}, separators=(",", ":")))
    request.chmod(0o600)

    first = subprocess.run([find_bash(), str(helper)], cwd=control, text=True, capture_output=True, check=False)

    assert first.returncode == 0, first.stderr
    assert not request.exists()
    assert (control / nonce).is_file()
    assert rebooted.is_file()
    rebooted.unlink()
    request.write_text(json.dumps({"action": "reboot", "nonce": nonce, "version": 1}, separators=(",", ":")))
    request.chmod(0o600)
    replay = subprocess.run([find_bash(), str(helper)], cwd=control, text=True, capture_output=True, check=False)
    assert replay.returncode != 0
    assert not request.exists()
    assert not rebooted.exists()
    request.write_text('{"action":"reboot","nonce":"$(touch pwned)","version":1}')
    request.chmod(0o600)
    invalid = subprocess.run([find_bash(), str(helper)], cwd=tmp_path, text=True, capture_output=True, check=False)
    assert invalid.returncode != 0
    assert not (tmp_path / "pwned").exists()
    assert not rebooted.exists()


def test_reboot_helper_rejects_symlink_request_without_reboot(tmp_path: Path) -> None:
    helper, spool, _, rebooted = generated_reboot_helper(tmp_path)
    target = tmp_path / "outside-request"
    target.write_text('{"action":"reboot","nonce":"0123456789abcdef0123456789abcdef","version":1}')
    try:
        (spool / "request.json").symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")

    result = subprocess.run([find_bash(), str(helper)], text=True, capture_output=True, check=False)

    assert result.returncode != 0
    assert not rebooted.exists()
    assert target.exists()


def test_installer_provisions_and_enables_only_the_reboot_path_helper() -> None:
    installer = read_asset("deploy/install-on-ubuntu.sh")

    for asset in (
        "hermes-reboot-helper.sh",
        "hermes-reboot-helper.service",
        "hermes-reboot-helper.path",
    ):
        assert asset in installer
    assert "root hermes 0730" in installer
    assert "root root 0700" in installer
    assert 'root:hermes:730' in installer
    assert 'root:root:700' in installer
    assert 'systemd-analyze verify "$UNIT_STAGE" "$REBOOT_SERVICE_STAGE" "$REBOOT_PATH_STAGE"' in installer
    assert 'systemctl enable --now hermes-reboot-helper.path' in installer
    assert 'install -o root -g root -m "$mode"' in installer
    assert "apt-get install -y --no-install-recommends python3" in installer


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
        "XMPP_HOST": "xmpp.example.com",
        "XMPP_PORT": "5223",
        "XMPP_TLS_MODE": "direct",
        "XMPP_ADMIN_STATE_PATH": "/var/lib/hermes/.hermes/xmpp/admin.json",
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


def test_minimal_bootstrap_installer_configures_only_xmpp_and_first_owner() -> None:
    script = read_asset("installer.sh")

    for prompt in (
        "XMPP host",
        "XMPP port",
        "XMPP TLS mode",
        "Bot full JID with resource",
        "Bot nick",
        "XMPP password",
        "First owner bare JID",
    ):
        assert prompt in script
    for forbidden in ("Model:", "Endpoint:", "API token:", "Trusted JID:"):
        assert forbidden.casefold() not in script.casefold()
    assert "read -r -s" in script
    assert "mktemp" in script and "mv -f" in script and "chmod 0600" in script
    assert '"$STAGE/deploy/install-on-ubuntu.sh"' in script
    assert "systemctl disable --now hermes-gateway" in script
    assert 'version\\\\\\"' in script and 'owners\\\\\\"' in script and 'trusted_jids\\\\\\"' in script


def test_bootstrap_prompts_for_docker_before_exactly_seven_xmpp_values(tmp_path: Path) -> None:
    script, root, env = generated_bootstrap(tmp_path, docker_missing=True)

    result = subprocess.run(
        [find_bash(), str(script), "--ref", env["TEST_REF"]], cwd=ROOT, env=env,
        input="y\n" + bootstrap_input(), text=True, capture_output=True, check=False,
    )

    assert result.returncode == 0, result.stderr
    prompts = re.findall(r"(?:Docker Engine is not ready[^\n]*|XMPP host: |XMPP port: |XMPP TLS mode \(direct\): |Bot full JID with resource: |Bot nick: |XMPP password: |First owner bare JID: )", result.stdout + result.stderr)
    assert prompts == [
        "Docker Engine is not ready. Install Ubuntu package docker.io? [y/N] ",
        "XMPP host: ", "XMPP port: ", "XMPP TLS mode (direct): ",
        "Bot full JID with resource: ", "Bot nick: ", "XMPP password: ", "First owner bare JID: ",
    ]
    assert (root / "etc/hermes/hermes.env").is_file()
    assert (root / "var/lib/hermes/.hermes/xmpp/admin.json").is_file()


def test_bootstrap_rejects_control_character_jid_without_replacing_existing_generation(tmp_path: Path) -> None:
    script, root, env = generated_bootstrap(tmp_path)
    env_file = root / "etc/hermes/hermes.env"
    state_file = root / "var/lib/hermes/.hermes/xmpp/admin.json"
    env_file.write_text("old-env\n", encoding="utf-8")
    state_file.write_text('{"old":true}\n', encoding="utf-8")

    result = subprocess.run(
        [find_bash(), str(script), "--ref", env["TEST_REF"]], cwd=ROOT, env=env,
        input=bootstrap_input(owner="owner\x01@example.com"), text=True, capture_output=True, check=False,
    )

    assert result.returncode != 0
    assert env_file.read_text(encoding="utf-8") == "old-env\n"
    assert state_file.read_text(encoding="utf-8") == '{"old":true}\n'


def test_bootstrap_restores_both_previous_files_when_state_commit_fails(tmp_path: Path) -> None:
    script, root, env = generated_bootstrap(tmp_path, fail_state_commit=True)
    env_file = root / "etc/hermes/hermes.env"
    state_file = root / "var/lib/hermes/.hermes/xmpp/admin.json"
    env_file.write_text("old-env\n", encoding="utf-8")
    state_file.write_text('{"version":1,"owners":["old@example.com"]}\n', encoding="utf-8")

    result = subprocess.run(
        [find_bash(), str(script), "--ref", env["TEST_REF"]], cwd=ROOT, env=env,
        input=bootstrap_input(), text=True, capture_output=True, check=False,
    )

    assert result.returncode != 0
    assert env_file.read_text(encoding="utf-8") == "old-env\n"
    assert state_file.read_text(encoding="utf-8") == '{"version":1,"owners":["old@example.com"]}\n'
    assert not (root / "etc/hermes/.xmpp-config-transaction").exists()


def test_bootstrap_backup_failure_preserves_the_live_previous_generation(tmp_path: Path) -> None:
    script, root, env = generated_bootstrap(tmp_path, fail_env_backup=True)
    env_file = root / "etc/hermes/hermes.env"
    state_file = root / "var/lib/hermes/.hermes/xmpp/admin.json"
    env_file.write_text("old-env\n", encoding="utf-8")
    state_file.write_text('{"version":1,"owners":["old@example.com"]}\n', encoding="utf-8")

    result = subprocess.run(
        [find_bash(), str(script), "--ref", env["TEST_REF"]], cwd=ROOT, env=env,
        input=bootstrap_input(), text=True, capture_output=True, check=False,
    )

    assert result.returncode != 0
    assert env_file.read_text(encoding="utf-8") == "old-env\n"
    assert state_file.read_text(encoding="utf-8") == '{"version":1,"owners":["old@example.com"]}\n'


def test_bootstrap_recovery_preserves_a_durably_committed_new_generation(tmp_path: Path) -> None:
    script, root, env = generated_bootstrap(tmp_path, recover_only=True)
    env_file = root / "etc/hermes/hermes.env"
    state_dir = root / "var/lib/hermes/.hermes/xmpp"
    state_file = state_dir / "admin.json"
    transaction = root / "etc/hermes/.xmpp-config-transaction"
    transaction.mkdir()
    env_file.write_text("new-env\n", encoding="utf-8")
    state_file.write_text('{"version":1,"owners":["new@example.com"]}\n', encoding="utf-8")
    (transaction / "env.present").touch()
    (transaction / "env.old").write_text("old-env\n", encoding="utf-8")
    (transaction / "state.present").touch()
    (transaction / "state.old").write_text(
        '{"version":1,"owners":["old@example.com"]}\n', encoding="utf-8"
    )
    (transaction / ".commit").touch()

    result = subprocess.run(
        [find_bash(), str(script), "--ref", env["TEST_REF"]], cwd=ROOT, env=env,
        input=bootstrap_input(), text=True, capture_output=True, check=False,
    )

    assert result.returncode == 0, result.stderr
    assert env_file.read_text(encoding="utf-8") == "new-env\n"
    assert state_file.read_text(encoding="utf-8") == '{"version":1,"owners":["new@example.com"]}\n'
    assert not transaction.exists()
    assert not (transaction / "state.old").exists()


@pytest.mark.parametrize("interruption", ("HUP", "INT", "TERM"))
def test_bootstrap_interruption_after_durable_commit_preserves_new_generation(
    tmp_path: Path, interruption: str,
) -> None:
    script, root, env = generated_bootstrap(tmp_path, interrupt_after_commit_marker=interruption)
    env_file = root / "etc/hermes/hermes.env"
    state_file = root / "var/lib/hermes/.hermes/xmpp/admin.json"
    env_file.write_text("old-env\n", encoding="utf-8")
    state_file.write_text('{"version":1,"owners":["old@example.com"]}\n', encoding="utf-8")

    result = subprocess.run(
        [find_bash(), str(script), "--ref", env["TEST_REF"]], cwd=ROOT, env=env,
        input=bootstrap_input(), text=True, capture_output=True, check=False,
    )

    assert result.returncode != 0, result.stderr
    written_env = env_file.read_text(encoding="utf-8")
    assert written_env != "old-env\n"
    assert "XMPP_JID=\"bot@example.com/Hermes\"\n" in written_env
    assert "XMPP_ALLOWED_USERS=\"owner@example.com\"\n" in written_env
    assert "XMPP_PASSWORD=\"secret\"\n" in written_env
    assert state_file.read_text(encoding="utf-8") == (
        '{"version":1,"revision":0,"owners":["owner@example.com"],'
        '"trusted_jids":[],"model":null,"endpoint":null,"token":null}\n'
    )
    assert not (root / "etc/hermes/.xmpp-config-transaction").exists()


def test_bootstrap_recovery_restores_a_complete_old_generation_after_interruption(tmp_path: Path) -> None:
    script, root, env = generated_bootstrap(tmp_path, recover_only=True)
    env_file = root / "etc/hermes/hermes.env"
    state_dir = root / "var/lib/hermes/.hermes/xmpp"
    state_file = state_dir / "admin.json"
    transaction = root / "etc/hermes/.xmpp-config-transaction"
    transaction.mkdir()
    env_file.write_text("new-env\n", encoding="utf-8")
    state_file.write_text('{"version":1,"owners":["old@example.com"]}\n', encoding="utf-8")
    (transaction / "env.present").touch()
    (transaction / "env.old").write_text("old-env\n", encoding="utf-8")
    (transaction / "state.present").touch()
    (transaction / "state.old").write_text(
        '{"version":1,"owners":["old@example.com"]}\n', encoding="utf-8"
    )
    (transaction / "backups.ready").touch()

    result = subprocess.run(
        [find_bash(), str(script), "--ref", env["TEST_REF"]], cwd=ROOT, env=env,
        input=bootstrap_input(), text=True, capture_output=True, check=False,
    )

    assert result.returncode == 0, result.stderr
    assert env_file.read_text(encoding="utf-8") == "old-env\n"
    assert state_file.read_text(encoding="utf-8") == '{"version":1,"owners":["old@example.com"]}\n'
    assert not transaction.exists()


def test_bootstrap_success_removes_all_transaction_backups(tmp_path: Path) -> None:
    script, root, env = generated_bootstrap(tmp_path)
    env_file = root / "etc/hermes/hermes.env"
    state_dir = root / "var/lib/hermes/.hermes/xmpp"
    state_file = state_dir / "admin.json"
    env_file.write_text("old-env\n", encoding="utf-8")
    state_file.write_text('{"version":1,"owners":["old@example.com"]}\n', encoding="utf-8")

    result = subprocess.run(
        [find_bash(), str(script), "--ref", env["TEST_REF"]], cwd=ROOT, env=env,
        input=bootstrap_input(), text=True, capture_output=True, check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not (root / "etc/hermes/.xmpp-config-transaction").exists()
    assert not any(state_dir.glob(".admin.json.*"))


def test_bootstrap_serializes_the_env_and_admin_state_generation_under_one_lock() -> None:
    script = read_asset("installer.sh")

    assert 'exec 9>"$ENV_DIR/.xmpp-config.lock"' in script
    assert "flock -x 9" in script
    assert "TXN_DIR=$ENV_DIR/.xmpp-config-transaction" in script
    assert 'mv -f "$ENV_TMP" "$ENV_FILE"' in script
    assert 'mv -f "$STATE_TMP" "$STATE_FILE"' in script
    assert 'transaction_cleanup()' in script


def test_bootstrap_blocks_a_second_generation_until_the_first_releases_the_lock(tmp_path: Path) -> None:
    script, root, env = generated_bootstrap(tmp_path, pause_lock=True)
    first = subprocess.Popen(
        [find_bash(), str(script), "--ref", env["TEST_REF"]], cwd=ROOT, env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    assert first.stdin is not None
    first.stdin.write(bootstrap_input(owner="first@example.com"))
    first.stdin.close()
    ready = tmp_path / "lock.ready"
    for _ in range(100):
        if ready.exists():
            break
        import time
        time.sleep(0.02)
    assert ready.exists()

    second = subprocess.Popen(
        [find_bash(), str(script), "--ref", env["TEST_REF"]], cwd=ROOT, env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    assert second.stdin is not None
    second.stdin.write(bootstrap_input(owner="second@example.com"))
    second.stdin.close()
    import time
    time.sleep(0.1)
    assert second.poll() is None
    (tmp_path / "lock.release").touch()
    assert first.wait(timeout=10) == 0, first.stderr.read() if first.stderr else ""
    assert second.wait(timeout=10) == 0, second.stderr.read() if second.stderr else ""
    assert 'second@example.com' in (root / "etc/hermes/hermes.env").read_text(encoding="utf-8")
    assert json.loads((root / "var/lib/hermes/.hermes/xmpp/admin.json").read_text(encoding="utf-8"))["owners"] == ["second@example.com"]


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
    assert 'secure_dir "$HERMES_ACCOUNT_HOME_DISK" root root 0755' in script
    assert 'secure_runtime_root "$HERMES_LOCAL_DISK"' in script
    assert 'secure_runtime_root "$HERMES_HOME_DISK"' in script


def test_root_never_traverses_hermes_owned_runtime_paths() -> None:
    script = read_asset("deploy/install-on-ubuntu.sh")
    root_dir_checks = re.findall(r'^reject_unsafe_existing_dir "\$(\w+)"$', script, re.MULTILINE)
    root_file_checks = re.findall(r'^reject_unsafe_existing_file "\$(\w+)"$', script, re.MULTILINE)
    assert root_dir_checks == ["HERMES_ACCOUNT_HOME_DISK"] * 3 + ["REBOOT_SPOOL_DIR", "REBOOT_CONTROL_DIR"]
    assert root_file_checks == []
    descendant_preflight = script.index("runuser -u hermes -- sh -c '")
    dependency_install = 'apt-get install -y --no-install-recommends ca-certificates curl git build-essential pkg-config libssl-dev libffi-dev'
    assert descendant_preflight < script.index(dependency_install)
    preflight_call = script[descendant_preflight:script.index(dependency_install)]
    for variable in (
        "HERMES_LOCAL_DISK", "HERMES_HOME_DISK", "HERMES_CACHE_DISK", "PLUGIN_PARENT",
        "HERMES_AGENT_DISK", "HERMES_VENV_DISK", "HERMES_VENV_BIN_DISK",
        "HERMES_BIN_PARENT_DISK", "HERMES_BIN_DISK", "HERMES_PYTHON_DISK", "UV_BIN_DISK",
    ):
        assert f'"${variable}"' in preflight_call


@pytest.mark.parametrize(
    ("marker", "old_asset"),
    [
        ('PLUGIN_BACKED_UP=1', "plugin"),
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


def test_ready_docker_skips_installation_preflight(tmp_path: Path) -> None:
    result, _, env = run_installer(tmp_path)

    assert result.returncode == 0, result.stderr
    log = Path(env["HERMES_TEST_LOG"].replace("/c/", "C:/") if os.name == "nt" else env["HERMES_TEST_LOG"]).read_text()
    assert "apt-get install -y --no-install-recommends docker.io" not in log


@pytest.mark.parametrize("answer", ["no\n", "\n"])
def test_missing_docker_requires_explicit_installation_consent(tmp_path: Path, answer: str) -> None:
    fake_root, env = build_harness(tmp_path)
    env["HERMES_TEST_DOCKER_READY"] = "0"
    script = generated_installer(tmp_path, fake_root, interactive_stdin=True)

    result = subprocess.run(
        [find_bash(), str(script)], cwd=ROOT, env=env, input=answer,
        text=True, capture_output=True, check=False,
    )

    assert result.returncode != 0
    log_path = tmp_path / "commands.log"
    assert not log_path.exists() or "apt-get" not in log_path.read_text()
    assert not (fake_root / "etc/hermes/hermes.env").exists()


@pytest.mark.parametrize("answer", ["yes\n", "ДА\n", "дА\n"])
def test_missing_docker_installs_after_explicit_consent(tmp_path: Path, answer: str) -> None:
    fake_root, env = build_harness(tmp_path)
    env["HERMES_TEST_DOCKER_READY"] = "0"
    script = generated_installer(tmp_path, fake_root, interactive_stdin=True)

    result = subprocess.run(
        [find_bash(), str(script)], cwd=ROOT, env=env, input=answer.encode("utf-8"),
        capture_output=True, check=False,
    )

    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    log = (tmp_path / "commands.log").read_text()
    assert "apt-get update" in log
    assert "apt-get install -y --no-install-recommends docker.io" in log
    assert "systemctl enable --now docker" in log
    assert log.index("systemctl enable --now docker") < log.index("groupadd --system hermes")


def test_missing_docker_rejects_noninteractive_stdin(tmp_path: Path) -> None:
    result, root, env = run_installer(tmp_path, extra={"HERMES_TEST_DOCKER_READY": "0"})

    assert result.returncode != 0
    log_path = Path(env["HERMES_TEST_LOG"].replace("/c/", "C:/") if os.name == "nt" else env["HERMES_TEST_LOG"])
    assert not log_path.exists() or "apt-get" not in log_path.read_text()
    assert not (root / "etc/hermes/hermes.env").exists()


@pytest.mark.parametrize("failure", ["HERMES_TEST_FAIL_DOCKER_INSTALL", "HERMES_TEST_FAIL_DOCKER_DAEMON"])
def test_missing_docker_fails_when_installation_does_not_make_it_ready(tmp_path: Path, failure: str) -> None:
    fake_root, env = build_harness(tmp_path)
    env.update(HERMES_TEST_DOCKER_READY="0", **{failure: "1"})
    script = generated_installer(tmp_path, fake_root, interactive_stdin=True)

    result = subprocess.run(
        [find_bash(), str(script)], cwd=ROOT, env=env, input="y\n",
        text=True, capture_output=True, check=False,
    )

    assert result.returncode != 0
    assert not (fake_root / "etc/hermes/hermes.env").exists()


@pytest.mark.parametrize("answer", ["yes\n", "ДА\n", "дА\n"])
def test_service_starts_only_after_doctor_and_explicit_consent(tmp_path: Path, answer: str) -> None:
    fake_root, env = build_harness(tmp_path)
    script = generated_installer(tmp_path, fake_root, interactive_stdin=True)

    result = subprocess.run(
        [find_bash(), str(script)], cwd=ROOT, env=env, input=answer.encode("utf-8"),
        capture_output=True, check=False,
    )

    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    state = tmp_path / "state"
    assert (state / "active").exists() and (state / "enabled").exists()
    log = (tmp_path / "commands.log").read_text()
    assert "runuser -u hermes -- /" in log
    assert "systemctl cat hermes-gateway" in log
    assert "systemctl enable --now hermes-gateway" in log
    assert log.index("systemctl cat hermes-gateway") < log.index("systemctl enable --now hermes-gateway")


@pytest.mark.parametrize("answer", ["no\n", "\n", ""])
def test_service_remains_stopped_and_disabled_without_consent(tmp_path: Path, answer: str) -> None:
    fake_root, env = build_harness(tmp_path)
    script = generated_installer(tmp_path, fake_root, interactive_stdin=True)

    result = subprocess.run(
        [find_bash(), str(script)], cwd=ROOT, env=env, input=answer,
        text=True, capture_output=True, check=False,
    )

    assert result.returncode == 0, result.stderr
    state = tmp_path / "state"
    assert not (state / "active").exists() and not (state / "enabled").exists()


@pytest.mark.parametrize("failure", ["HERMES_TEST_FAIL_DOCTOR", "HERMES_TEST_MISSING_UNIT", "HERMES_TEST_FAIL_ENABLE_NOW", "HERMES_TEST_FALSE_ACTIVE", "HERMES_TEST_FALSE_ENABLE"])
def test_service_start_failures_fail_without_exposing_env(tmp_path: Path, failure: str) -> None:
    fake_root, env = build_harness(tmp_path)
    env[failure] = "1"
    script = generated_installer(tmp_path, fake_root, interactive_stdin=True)

    result = subprocess.run(
        [find_bash(), str(script)], cwd=ROOT, env=env, input="y\n",
        text=True, capture_output=True, check=False,
    )

    assert result.returncode != 0
    assert "XMPP_PASSWORD" not in result.stdout
    assert "XMPP_PASSWORD" not in result.stderr
    log = (tmp_path / "commands.log").read_text()
    if failure in {"HERMES_TEST_FAIL_ENABLE_NOW", "HERMES_TEST_FALSE_ACTIVE", "HERMES_TEST_FALSE_ENABLE"}:
        assert "systemctl status --no-pager hermes-gateway" in log


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
            "adapter.py", "plugin.yaml", "xmpp_bridge/__init__.py", "xmpp_bridge/admin_state.py", "xmpp_bridge/client.py",
            "xmpp_bridge/commands.py", "xmpp_bridge/models.py", "xmpp_bridge/policy.py", "xmpp_bridge/reboot.py",
            "xmpp_bridge/state.py", "xmpp_bridge/updates.py",
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


@pytest.mark.parametrize("unsafe_kind", ["symlink", "dangling_symlink", "file"])
def test_unsafe_account_home_fails_before_apt(tmp_path: Path, unsafe_kind: str) -> None:
    fake_root, env = build_harness(tmp_path)
    account_home = fake_root / "var/lib/hermes"
    external = tmp_path / "external-account-home"
    external.mkdir()
    marker = external / "marker"
    marker.write_text("unchanged")
    before_mode = stat.S_IMODE(external.stat().st_mode)
    try:
        if unsafe_kind == "symlink":
            account_home.parent.mkdir(parents=True)
            account_home.symlink_to(external, target_is_directory=True)
        elif unsafe_kind == "dangling_symlink":
            account_home.parent.mkdir(parents=True)
            account_home.symlink_to(tmp_path / "missing-account-home", target_is_directory=True)
        else:
            account_home.parent.mkdir(parents=True)
            account_home.write_text("not a directory")
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


def test_account_home_race_cannot_redirect_privileged_metadata_changes(tmp_path: Path) -> None:
    fake_root, env = build_harness(tmp_path)
    account_home = fake_root / "var/lib/hermes"
    account_home.mkdir(parents=True)
    external = tmp_path / "race-external"
    external.mkdir()
    marker = external / "marker"
    marker.write_text("unchanged")
    external.chmod(0o755)
    before_mode = stat.S_IMODE(external.stat().st_mode)
    try:
        (tmp_path / "race-link").symlink_to(external, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")
    generated = generated_installer(
        tmp_path,
        fake_root,
        race_after="apt-get install -y --no-install-recommends ca-certificates curl git build-essential pkg-config libssl-dev libffi-dev",
    )

    result = subprocess.run([find_bash(), str(generated)], cwd=ROOT, env=env, text=True, capture_output=True)

    assert result.returncode != 0
    assert marker.read_text() == "unchanged"
    assert stat.S_IMODE(external.stat().st_mode) == before_mode
    assert not (fake_root / "etc/hermes/hermes.env").exists()
    assert not (fake_root / "etc/systemd/system/hermes-gateway.service").exists()


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
    recursive_chowns = [line for line in log.splitlines() if line.startswith("chown -R hermes:hermes ")]
    assert len(recursive_chowns) == 1
    assert "/var/lib/hermes/.xmpp-source." in recursive_chowns[0]
    assert "/var/lib/hermes/.hermes/" not in recursive_chowns[0]
    assert "runuser -u hermes -- sh -c " in log


def test_bash_syntax() -> None:
    result = subprocess.run([find_bash(), "-n", "deploy/install-on-ubuntu.sh"], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_bootstrap_has_a_pinned_default_release_for_one_line_install() -> None:
    script = read_asset("installer.sh")
    assert "REF=${HERMES_INSTALL_REF:-main}" in script
    assert "invalid Git branch or tag" in script
    assert "rev-parse FETCH_HEAD" in script


def test_bootstrap_retries_jid_and_password_after_failed_service_start() -> None:
    script = read_asset("installer.sh")
    assert "JID или пароль могут быть неверными" in script
    assert "Bot full JID with resource: " in script
    assert "XMPP password: " in script
    assert "write_xmpp_env" in script


def test_bootstrap_reads_pipeline_prompts_from_terminal() -> None:
    script = read_asset("installer.sh")
    assert "exec 3</dev/tty" in script
    assert "[ -t 1 ]" in script
    assert "read_line" in script
    assert "HERMES_INSTALLER_REEXEC" in script
    assert 'exec bash "$SELF_TMP" "$@" </dev/tty' in script


def test_bootstrap_collects_xmpp_data_before_the_upstream_installer() -> None:
    script = read_asset("installer.sh")
    assert script.index("XMPP configuration:") < script.index("HERMES_DEFER_SERVICE_START=1 bash")
    assert script.index("preflight_read_value 'Сервер XMPP: '") < script.index("HERMES_DEFER_SERVICE_START=1 bash")
    assert script.index("preflight_read_value 'Сервер XMPP: '") < script.index("Installing bootstrap dependencies...")


def test_bootstrap_derives_nick_from_the_jid_resource() -> None:
    script = read_asset("installer.sh")
    assert "NICK=${JID#*/}" in script
    assert "Имя бота (ник):" not in script


def test_bootstrap_reports_download_and_configuration_stages() -> None:
    script = read_asset("installer.sh")
    assert "Installing bootstrap dependencies..." in script
    assert "Downloading project files..." in script
    assert "timeout 120 env GIT_TERMINAL_PROMPT=0 git" in script
    assert "Preparing XMPP configuration..." in script
    assert "Checking Docker Engine..." in script
    assert "timeout 15 docker info" in script


def test_unix_deployment_assets_are_forced_to_lf_in_archives() -> None:
    attributes = read_asset(".gitattributes").splitlines()
    assert "deploy/*.sh text eol=lf" in attributes
    assert "deploy/*.service text eol=lf" in attributes
    assert "deploy/*.example text eol=lf" in attributes


def test_readme_documents_assisted_installation_and_support_disclosure() -> None:
    readme = read_asset("README.md")
    required = (
        "Docker Engine",
        "docker.io",
        "XMPP host",
        "First owner bare JID",
        "hermes doctor",
        "hermes-gateway",
        "hermes-reboot-helper",
        "без OMEMO/E2E",
        "https://aitunnel.ru?r=43877",
    )
    for phrase in required:
        assert phrase in readme
    assert "РџР»Р°РіРёРЅ" not in readme
    assert "sudo bash deploy/install-on-ubuntu.sh" in readme
