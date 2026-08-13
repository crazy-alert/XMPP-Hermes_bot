#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_PREFIX=""
if [ "$(id -u)" -ne 0 ]; then
    printf '%s\n' 'Ошибка: запустите установщик от root.' >&2
    exit 1
fi

path_at() {
    printf '%s%s\n' "$ROOT_PREFIX" "$1"
}

OS_RELEASE=$(path_at /etc/os-release)
if [ ! -r "$OS_RELEASE" ]; then
    printf '%s\n' 'Ошибка: не найден /etc/os-release.' >&2
    exit 1
fi
# shellcheck disable=SC1090
. "$OS_RELEASE"
if [ "${ID:-}" != ubuntu ]; then
    printf '%s\n' 'Ошибка: требуется Ubuntu 24.04 или новее.' >&2
    exit 1
fi
if ! dpkg --compare-versions "${VERSION_ID:-0}" ge 24.04; then
    printf '%s\n' 'Ошибка: требуется Ubuntu 24.04 или новее.' >&2
    exit 1
fi

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
REPO_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd -P)
PLUGIN_SOURCE=$REPO_DIR/hermes-xmpp
HERMES_HOME=/var/lib/hermes/.hermes
HERMES_AGENT_DIR=$HERMES_HOME/hermes-agent
HERMES_HOME_DISK=$(path_at "$HERMES_HOME")
HERMES_AGENT_DISK=$(path_at "$HERMES_AGENT_DIR")
HERMES_BIN=$HERMES_AGENT_DIR/venv/bin/hermes
HERMES_PYTHON=$HERMES_AGENT_DIR/venv/bin/python
UV_BIN=$HERMES_HOME/bin/uv
HERMES_BIN_DISK=$(path_at "$HERMES_BIN")
HERMES_PYTHON_DISK=$(path_at "$HERMES_PYTHON")
UV_BIN_DISK=$(path_at "$UV_BIN")
PLUGIN_DEST=$(path_at /var/lib/hermes/.hermes/plugins/xmpp-platform)
ENV_FILE=$(path_at /etc/hermes/hermes.env)
UNIT_FILE=$(path_at /etc/systemd/system/hermes-gateway.service)

HERMES_RELEASE=v2026.8.3
HERMES_COMMIT=3c27eb6234bf91b8ceee9e9071591b31e9b148cb
INSTALLER_SHA256=45f589461248c7a6ec3aecd7522a69dd49c5c8dbf4798ba1296af5c0c5e7ccd3
OFFICIAL_INSTALLER_URL=https://raw.githubusercontent.com/NousResearch/hermes-agent/$HERMES_COMMIT/scripts/install.sh

if [ ! -f "$PLUGIN_SOURCE/adapter.py" ] || [ ! -f "$PLUGIN_SOURCE/plugin.yaml" ] || [ ! -d "$PLUGIN_SOURCE/xmpp_bridge" ]; then
    printf 'Ошибка: неполный источник плагина: %s\n' "$PLUGIN_SOURCE" >&2
    exit 1
fi
if find "$PLUGIN_SOURCE" \( -type l -o \( ! -type d ! -type f \) \) -print -quit | grep -q .; then
    printf '%s\n' 'Ошибка: источник плагина содержит ссылку или нерегулярный файл.' >&2
    exit 1
fi
if ! getent group docker >/dev/null; then
    printf '%s\n' 'Ошибка: группа docker отсутствует; сначала установите Docker Engine.' >&2
    exit 1
fi

# Existing identities are validated before apt or any other host mutation.
if getent group hermes >/dev/null; then
    if ! getent passwd hermes >/dev/null || [ "$(id -gn hermes)" != hermes ]; then
        printf '%s\n' 'Ошибка: существующая группа hermes конфликтует с учётной записью.' >&2
        exit 1
    fi
fi
if getent passwd hermes >/dev/null; then
    HERMES_ACCOUNT_HOME=$(getent passwd hermes | cut -d: -f6)
    if [ "$HERMES_ACCOUNT_HOME" != /var/lib/hermes ] || [ "$(id -gn hermes)" != hermes ]; then
        printf '%s\n' 'Ошибка: ожидаются hermes:hermes и home /var/lib/hermes.' >&2
        exit 1
    fi
fi

HERMES_LOCAL_DISK=$(path_at /var/lib/hermes/.local)
if [ -e "$HERMES_LOCAL_DISK" ] || [ -L "$HERMES_LOCAL_DISK" ]; then
    if [ ! -d "$HERMES_LOCAL_DISK" ] || [ -L "$HERMES_LOCAL_DISK" ]; then
        printf '%s\n' 'Ошибка: /var/lib/hermes/.local должен быть обычным каталогом, а не ссылкой или файлом.' >&2
        exit 1
    fi
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends ca-certificates curl git build-essential pkg-config libssl-dev libffi-dev

if ! getent group hermes >/dev/null; then groupadd --system hermes; fi
if ! getent passwd hermes >/dev/null; then
    useradd --system --create-home --home-dir /var/lib/hermes --gid hermes --shell /usr/sbin/nologin hermes
fi

secure_dir() {
    install -d -o "$2" -g "$3" -m "$4" "$1"
}
secure_hermes_dir() {
    directory=$1
    runuser -u hermes -- mkdir -p -- "$directory"
    chown --no-dereference hermes:hermes "$directory"
    if [ ! -d "$directory" ] || [ -L "$directory" ] || [ "$(stat -c %U "$directory")" != hermes ]; then
        printf 'Ошибка: небезопасный каталог Hermes: %s\n' "$directory" >&2
        exit 1
    fi
    runuser -u hermes -- chmod 0700 -- "$directory"
}
secure_dir "$(path_at /var/lib/hermes)" hermes hermes 0700
secure_hermes_dir "$HERMES_LOCAL_DISK"
secure_hermes_dir "$(path_at /var/lib/hermes/.local/bin)"
secure_dir "$HERMES_HOME_DISK" hermes hermes 0700
secure_dir "$(dirname -- "$PLUGIN_DEST")" hermes hermes 0700
secure_dir "$(dirname -- "$ENV_FILE")" root root 0750
secure_dir "$(dirname -- "$UNIT_FILE")" root root 0755

INSTALLER_TMP=$(mktemp "$(path_at /var/lib/hermes)/.hermes-installer.XXXXXX")
PLUGIN_STAGE=$(mktemp -d "$(dirname -- "$PLUGIN_DEST")/.xmpp-platform.stage.XXXXXX")
UNIT_STAGE=$(mktemp "$(dirname -- "$UNIT_FILE")/.hermes-gateway.stage.XXXXXX")
PLUGIN_BACKUP=$PLUGIN_DEST.backup.$$
UNIT_BACKUP=$UNIT_FILE.backup.$$
PLUGIN_SWAPPED=0
UNIT_SWAPPED=0
PLUGIN_BACKED_UP=0
UNIT_BACKED_UP=0
TRANSACTION=0
WAS_ACTIVE=0
WAS_ENABLED=0

rollback() {
    status=$?
    trap - EXIT
    if [ "$TRANSACTION" = 1 ] && [ "$status" -ne 0 ]; then
        if [ "$UNIT_SWAPPED" = 1 ]; then rm -f -- "$UNIT_FILE"; fi
        if [ "$UNIT_BACKED_UP" = 1 ] && [ -e "$UNIT_BACKUP" ]; then mv -- "$UNIT_BACKUP" "$UNIT_FILE"; fi
        if [ "$PLUGIN_SWAPPED" = 1 ]; then rm -rf -- "$PLUGIN_DEST"; fi
        if [ "$PLUGIN_BACKED_UP" = 1 ] && [ -e "$PLUGIN_BACKUP" ]; then mv -- "$PLUGIN_BACKUP" "$PLUGIN_DEST"; fi
        systemctl daemon-reload >/dev/null 2>&1 || true
        [ "$WAS_ENABLED" = 0 ] || systemctl enable hermes-gateway >/dev/null 2>&1 || true
        [ "$WAS_ACTIVE" = 0 ] || systemctl start hermes-gateway >/dev/null 2>&1 || true
    fi
    rm -rf -- "$INSTALLER_TMP" "$PLUGIN_STAGE" "$UNIT_STAGE"
    exit "$status"
}
trap rollback EXIT

validate_runtime() {
    for executable in "$HERMES_BIN_DISK" "$HERMES_PYTHON_DISK" "$UV_BIN_DISK"; do
        [ -f "$executable" ] && [ ! -L "$executable" ] && [ -x "$executable" ] || return 1
    done
    case "$HERMES_BIN_DISK:$HERMES_PYTHON_DISK:$UV_BIN_DISK" in /var/lib/hermes/*) : ;; *) return 1 ;; esac
    [ -z "$(find /var/lib/hermes -xdev ! -user hermes -print -quit)" ] || return 1
    "$HERMES_PYTHON_DISK" -c 'import sys; raise SystemExit(0 if sys.prefix != sys.base_prefix else 1)'
    "$HERMES_BIN_DISK" --version >/dev/null
    "$UV_BIN_DISK" --version >/dev/null
}

# Runtime installation is separate and idempotent. Docker access is granted only after validation.
if ! validate_runtime; then
    curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location "$OFFICIAL_INSTALLER_URL" --output "$INSTALLER_TMP"
    printf '%s  %s\n' "$INSTALLER_SHA256" "$INSTALLER_TMP" | sha256sum --check --status
    chown hermes:hermes "$INSTALLER_TMP"
    chmod 0700 "$INSTALLER_TMP"
    runuser -u hermes -- env HOME="$(path_at /var/lib/hermes)" HERMES_HOME="$HERMES_HOME_DISK" \
        bash "$INSTALLER_TMP" --skip-setup --skip-browser --dir "$HERMES_AGENT_DISK" \
        --hermes-home "$HERMES_HOME_DISK" --commit "$HERMES_COMMIT"
    validate_runtime || { printf '%s\n' 'Ошибка: установленная среда Hermes не прошла проверку.' >&2; exit 1; }
fi
runuser -u hermes -- "$UV_BIN_DISK" pip install --python "$HERMES_PYTHON_DISK" 'slixmpp>=1.12,<2' pytest

# Stage the complete allowlisted plugin before stopping the service.
cp -- "$PLUGIN_SOURCE/adapter.py" "$PLUGIN_SOURCE/plugin.yaml" "$PLUGIN_STAGE/"
mkdir -p -- "$PLUGIN_STAGE/xmpp_bridge"
while IFS= read -r source_file; do cp -- "$source_file" "$PLUGIN_STAGE/xmpp_bridge/"; done < <(find "$PLUGIN_SOURCE/xmpp_bridge" -maxdepth 1 -type f -name '*.py' -print)
[ -f "$PLUGIN_STAGE/xmpp_bridge/__init__.py" ] || { printf '%s\n' 'Ошибка: отсутствует xmpp_bridge/__init__.py.' >&2; exit 1; }
chown -R hermes:hermes "$PLUGIN_STAGE"
chmod -R go-rwx "$PLUGIN_STAGE"

sed "s|^ExecStart=.*|ExecStart=$HERMES_BIN gateway run|" "$SCRIPT_DIR/hermes-gateway.service" >"$UNIT_STAGE"
chmod 0644 "$UNIT_STAGE"
systemd-analyze verify "$UNIT_STAGE"

# Preserve env content, reject unsafe file types, and enforce manager-only secrets.
if [ -e "$ENV_FILE" ] || [ -L "$ENV_FILE" ]; then
    [ -f "$ENV_FILE" ] && [ ! -L "$ENV_FILE" ] || { printf '%s\n' 'Ошибка: hermes.env должен быть регулярным файлом.' >&2; exit 1; }
else
    cp -- "$SCRIPT_DIR/hermes.env.example" "$ENV_FILE"
fi
chown root:root "$ENV_FILE"
chmod 0600 "$ENV_FILE"

usermod -aG docker hermes

systemctl is-active --quiet hermes-gateway && WAS_ACTIVE=1 || true
systemctl is-enabled --quiet hermes-gateway && WAS_ENABLED=1 || true
TRANSACTION=1
if [ "$WAS_ACTIVE" = 1 ]; then systemctl stop hermes-gateway; fi

if [ -e "$PLUGIN_DEST" ]; then mv -- "$PLUGIN_DEST" "$PLUGIN_BACKUP"; PLUGIN_BACKED_UP=1; fi
mv -- "$PLUGIN_STAGE" "$PLUGIN_DEST"
PLUGIN_SWAPPED=1
if [ -e "$UNIT_FILE" ]; then mv -- "$UNIT_FILE" "$UNIT_BACKUP"; UNIT_BACKED_UP=1; fi
mv -- "$UNIT_STAGE" "$UNIT_FILE"
UNIT_SWAPPED=1
chown -R hermes:hermes "$PLUGIN_DEST"
chown root:root "$UNIT_FILE"
chmod -R go-rwx "$PLUGIN_DEST"
chmod 0644 "$UNIT_FILE"
systemctl daemon-reload
if [ "$WAS_ENABLED" = 1 ]; then systemctl disable hermes-gateway; fi

rm -rf -- "$PLUGIN_BACKUP"
rm -f -- "$UNIT_BACKUP"
TRANSACTION=0
printf 'Установка Hermes %s завершена. Служба остановлена и отключена.\n' "$HERMES_RELEASE"
