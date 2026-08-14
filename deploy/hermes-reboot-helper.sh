#!/usr/bin/env bash
set -Eeuo pipefail

SPOOL_DIR=/var/lib/hermes/reboot-spool
CONTROL_DIR=/var/lib/hermes/reboot-control
REQUEST_FILE=$SPOOL_DIR/request.json

if [ "$(id -u)" -ne 0 ]; then
    exit 1
fi

assert_root_owned_directory() {
    local directory=$1 mode
    [ -d "$directory" ] && [ ! -L "$directory" ] || return 1
    mode=$(stat -c '%a' -- "$directory")
    [ "$(stat -c '%U:%G' -- "$directory")" = root:root ] || return 1
    [ $((8#$mode & 8#022)) -eq 0 ]
}

assert_spool_directory() {
    [ -d "$SPOOL_DIR" ] && [ ! -L "$SPOOL_DIR" ] || return 1
    [ "$(stat -c '%U:%G:%a' -- "$SPOOL_DIR")" = root:hermes:730 ]
}

for directory in /var /var/lib /var/lib/hermes "$CONTROL_DIR"; do
    assert_root_owned_directory "$directory" || exit 1
done
assert_spool_directory || exit 1

if [ ! -e "$REQUEST_FILE" ] && [ ! -L "$REQUEST_FILE" ]; then
    exit 0
fi

if ! /usr/bin/python3 - "$REQUEST_FILE" "$CONTROL_DIR" <<'PY'
import grp
import json
import os
import pwd
import stat
import sys

request_path, control_dir = sys.argv[1:]


def discard_request() -> None:
    try:
        os.unlink(request_path)
    except FileNotFoundError:
        pass


valid = False
try:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(request_path, flags)
except OSError:
    discard_request()
    raise SystemExit(1)

try:
    metadata = os.fstat(fd)
    expected_uid = pwd.getpwnam("hermes").pw_uid
    expected_gid = grp.getgrnam("hermes").gr_gid
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_uid != expected_uid
        or metadata.st_gid != expected_gid
        or metadata.st_nlink != 1
        or metadata.st_size > 256
    ):
        raise ValueError("unsafe request metadata")
    raw = os.read(fd, 257)
    if len(raw) > 256:
        raise ValueError("request too large")
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict) or set(payload) != {"action", "nonce", "version"}:
        raise ValueError("invalid request schema")
    if payload.get("version") != 1 or payload.get("action") != "reboot":
        raise ValueError("invalid request action")
    nonce = payload.get("nonce")
    if not isinstance(nonce, str) or len(nonce) != 32 or any(char not in "0123456789abcdef" for char in nonce):
        raise ValueError("invalid request nonce")
    canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if raw != canonical:
        raise ValueError("non-canonical request")
    control_fd = os.open(control_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        marker_fd = os.open(
            nonce,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=control_fd,
        )
        os.close(marker_fd)
    finally:
        os.close(control_fd)
except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
    pass
else:
    valid = True
finally:
    os.close(fd)

discard_request()
if not valid:
    raise SystemExit(1)
PY
then
    exit 1
fi

/usr/bin/sleep 5
/usr/bin/systemctl reboot
