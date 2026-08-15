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
HERMES_ACCOUNT_HOME=/var/lib/hermes
HERMES_AGENT_DIR=$HERMES_HOME/hermes-agent
HERMES_ACCOUNT_HOME_DISK=$(path_at "$HERMES_ACCOUNT_HOME")
HERMES_HOME_DISK=$(path_at "$HERMES_HOME")
HERMES_AGENT_DISK=$(path_at "$HERMES_AGENT_DIR")
HERMES_BIN=$HERMES_AGENT_DIR/venv/bin/hermes
HERMES_PYTHON=$HERMES_AGENT_DIR/venv/bin/python
UV_BIN=$HERMES_HOME/bin/uv
HERMES_VENV_DISK=$(path_at "$HERMES_AGENT_DIR/venv")
HERMES_VENV_BIN_DISK=$(path_at "$HERMES_AGENT_DIR/venv/bin")
HERMES_BIN_PARENT_DISK=$(path_at "$HERMES_HOME/bin")
HERMES_BIN_DISK=$(path_at "$HERMES_BIN")
HERMES_PYTHON_DISK=$(path_at "$HERMES_PYTHON")
UV_BIN_DISK=$(path_at "$UV_BIN")
PLUGIN_DEST=$(path_at /var/lib/hermes/.hermes/plugins/xmpp-platform)
PLUGIN_CONFIG_FILE=$(path_at /var/lib/hermes/.hermes/config.yaml)
ENV_FILE=$(path_at /etc/hermes/hermes.env)
UNIT_FILE=$(path_at /etc/systemd/system/hermes-gateway.service)
REBOOT_HELPER_DISK=$(path_at /usr/local/libexec/hermes-reboot-helper)
REBOOT_SERVICE_FILE=$(path_at /etc/systemd/system/hermes-reboot-helper.service)
REBOOT_PATH_FILE=$(path_at /etc/systemd/system/hermes-reboot-helper.path)
REBOOT_SPOOL_DIR=$(path_at /var/lib/hermes/reboot-spool)
REBOOT_CONTROL_DIR=$(path_at /var/lib/hermes/reboot-control)
MANAGED_ENV_FILE=$(path_at /etc/hermes/.env)

HERMES_RELEASE=v2026.8.3
HERMES_COMMIT=3c27eb6234bf91b8ceee9e9071591b31e9b148cb
INSTALLER_SHA256=45f589461248c7a6ec3aecd7522a69dd49c5c8dbf4798ba1296af5c0c5e7ccd3
OFFICIAL_INSTALLER_URL=https://raw.githubusercontent.com/NousResearch/hermes-agent/$HERMES_COMMIT/scripts/install.sh

if [ ! -f "$PLUGIN_SOURCE/__init__.py" ] || [ ! -f "$PLUGIN_SOURCE/adapter.py" ] || [ ! -f "$PLUGIN_SOURCE/plugin.yaml" ] || [ ! -d "$PLUGIN_SOURCE/xmpp_bridge" ] || [ ! -f "$PLUGIN_SOURCE/xmpp_image_gen/__init__.py" ]; then
    printf 'Ошибка: неполный источник плагина: %s\n' "$PLUGIN_SOURCE" >&2
    exit 1
fi
for asset in hermes-reboot-helper.sh hermes-reboot-helper.service hermes-reboot-helper.path; do
    if [ ! -f "$SCRIPT_DIR/$asset" ] || [ -L "$SCRIPT_DIR/$asset" ]; then
        printf 'Error: missing safe reboot helper asset: %s\n' "$asset" >&2
        exit 1
    fi
done
if find "$PLUGIN_SOURCE" \( -type l -o \( ! -type d ! -type f \) \) -print -quit | grep -q .; then
    printf '%s\n' 'Ошибка: источник плагина содержит ссылку или нерегулярный файл.' >&2
    exit 1
fi
docker_ready() {
    command -v docker >/dev/null && getent group docker >/dev/null && docker info >/dev/null 2>&1
}

confirm_docker_installation() {
    if [ ! -t 0 ]; then
        printf '%s\n' 'Docker Engine is not ready and installation requires interactive input.' >&2
        return 1
    fi
    printf '%s' 'Docker Engine is not ready. Install Ubuntu package docker.io? [y/N] '
    IFS= read -r answer || return 1
    answer=${answer%$'\r'}
    case "$answer" in
        [Yy]|[Yy][Ee][Ss]|$'\320\264\320\260'|$'\320\264\320\220'|$'\320\224\320\260'|$'\320\224\320\220') return 0 ;;
        *) return 1 ;;
    esac
}

confirm_service_start() {
    if [ ! -t 0 ]; then
        return 1
    fi
    printf '%s' 'Enable automatic start and start Hermes now? [y/N] '
    IFS= read -r answer || return 1
    answer=${answer%$'\r'}
    case "$answer" in
        [Yy]|[Yy][Ee][Ss]|$'\320\264\320\260'|$'\320\264\320\220'|$'\320\224\320\260'|$'\320\224\320\220') return 0 ;;
        *) return 1 ;;
    esac
}

service_diagnostics() {
    systemctl status --no-pager hermes-gateway >&2 || true
    journalctl -u hermes-gateway --no-pager -n 50 >&2 || true
}

if ! docker_ready; then
    if ! confirm_docker_installation; then
        printf '%s\n' 'Hermes installation cancelled: Docker Engine is not ready.' >&2
        exit 1
    fi
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y --no-install-recommends docker.io
    systemctl enable --now docker
    if ! docker_ready; then
        printf '%s\n' 'Docker Engine failed the post-installation check.' >&2
        exit 1
    fi
fi

if ! docker_ready; then
    printf '%s\n' 'Ошибка: группа docker отсутствует; сначала установите Docker Engine.' >&2
    exit 1
fi

# Existing identities are validated before apt or any other host mutation.
reject_unsafe_existing_dir() {
    directory=$1
    if [ -e "$directory" ] || [ -L "$directory" ]; then
        if [ ! -d "$directory" ] || [ -L "$directory" ]; then
            printf 'Ошибка: путь Hermes должен быть обычным каталогом, а не ссылкой или файлом: %s\n' "$directory" >&2
            exit 1
        fi
    fi
}
reject_unsafe_existing_dir "$HERMES_ACCOUNT_HOME_DISK"

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
HERMES_CACHE_DISK=$(path_at /var/lib/hermes/.cache)
PLUGIN_PARENT=$(dirname -- "$PLUGIN_DEST")
if ! getent group hermes >/dev/null; then groupadd --system hermes; fi
if ! getent passwd hermes >/dev/null; then
    useradd --system --create-home --home-dir /var/lib/hermes --gid hermes --shell /usr/sbin/nologin hermes
fi

secure_dir() {
    install -d -o "$2" -g "$3" -m "$4" "$1"
}
secure_runtime_root() {
    directory=$1
    secure_dir "$directory" hermes hermes 0700
    if [ ! -d "$directory" ] || [ -L "$directory" ] || [ "$(stat -c %U "$directory")" != hermes ]; then
        printf 'Ошибка: небезопасный каталог Hermes: %s\n' "$directory" >&2
        exit 1
    fi
}
reject_unsafe_existing_dir "$HERMES_ACCOUNT_HOME_DISK"
secure_dir "$HERMES_ACCOUNT_HOME_DISK" root root 0755
if [ -L "$HERMES_ACCOUNT_HOME_DISK" ] || [ "$(stat -c %U:%G:%a "$HERMES_ACCOUNT_HOME_DISK")" != root:root:755 ]; then
    printf '%s\n' 'Ошибка: /var/lib/hermes не является доверенным каталогом root:root 0755.' >&2
    exit 1
fi
runuser -u hermes -- sh -c '
    for directory in "$1" "$2" "$3" "$4" "$5" "$6" "$7" "$8"; do
        if test -e "$directory" || test -L "$directory"; then
            test -d "$directory" && test ! -L "$directory" || exit 1
        fi
    done
    for file in "$9" "${10}" "${11}"; do
        if test -e "$file" || test -L "$file"; then
            if test -L "$file"; then
                resolved=$(readlink -f -- "$file") || exit 1
                case "$resolved" in "${12}"/*) : ;; *) exit 1 ;; esac
                test -f "$resolved" && test ! -L "$resolved" || exit 1
                test "$(stat -c %U:%G "$resolved")" = hermes:hermes || exit 1
            else
                test -f "$file" || exit 1
            fi
        fi
    done
' sh "$HERMES_LOCAL_DISK" "$HERMES_HOME_DISK" "$HERMES_CACHE_DISK" "$PLUGIN_PARENT" "$HERMES_AGENT_DISK" \
    "$HERMES_VENV_DISK" "$HERMES_VENV_BIN_DISK" "$HERMES_BIN_PARENT_DISK" \
    "$HERMES_BIN_DISK" "$HERMES_PYTHON_DISK" "$UV_BIN_DISK" "$HERMES_ACCOUNT_HOME_DISK" || {
    printf '%s\n' 'Ошибка: небезопасный существующий путь среды Hermes.' >&2
    exit 1
}

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends python3
apt-get install -y --no-install-recommends ca-certificates curl git build-essential pkg-config libssl-dev libffi-dev ripgrep ffmpeg

reject_unsafe_existing_dir "$HERMES_ACCOUNT_HOME_DISK"
if [ "$(stat -c %U:%G:%a "$HERMES_ACCOUNT_HOME_DISK")" != root:root:755 ]; then
    printf '%s\n' 'Ошибка: /var/lib/hermes перестал быть доверенным каталогом root:root 0755.' >&2
    exit 1
fi
reject_unsafe_existing_dir "$REBOOT_SPOOL_DIR"
reject_unsafe_existing_dir "$REBOOT_CONTROL_DIR"
secure_dir "$(dirname -- "$REBOOT_HELPER_DISK")" root root 0755
secure_dir "$REBOOT_SPOOL_DIR" root hermes 0730
secure_dir "$REBOOT_CONTROL_DIR" root root 0700
if [ -L "$REBOOT_SPOOL_DIR" ] || [ "$(stat -c %U:%G:%a "$REBOOT_SPOOL_DIR")" != root:hermes:730 ]; then
    printf '%s\n' 'Error: unsafe Hermes reboot spool.' >&2
    exit 1
fi
if [ -L "$REBOOT_CONTROL_DIR" ] || [ "$(stat -c %U:%G:%a "$REBOOT_CONTROL_DIR")" != root:root:700 ]; then
    printf '%s\n' 'Error: unsafe Hermes reboot control directory.' >&2
    exit 1
fi
secure_runtime_root "$HERMES_LOCAL_DISK"
secure_runtime_root "$HERMES_HOME_DISK"
secure_runtime_root "$HERMES_CACHE_DISK"
runuser -u hermes -- mkdir -p -- "$(path_at /var/lib/hermes/.local/bin)" "$PLUGIN_PARENT"
runuser -u hermes -- chmod 0700 -- "$(path_at /var/lib/hermes/.local/bin)" "$PLUGIN_PARENT"
secure_dir "$(dirname -- "$ENV_FILE")" root hermes 0750
secure_dir "$(dirname -- "$UNIT_FILE")" root root 0755

INSTALLER_TMP=$(mktemp "$(path_at /var/lib/hermes)/.hermes-installer.XXXXXX")
PLUGIN_SOURCE_STAGE=$(mktemp -d "$(path_at /var/lib/hermes)/.xmpp-source.XXXXXX")
PLUGIN_STAGE=$(runuser -u hermes -- mktemp -d "$(dirname -- "$PLUGIN_DEST")/.xmpp-platform.stage.XXXXXX")
UNIT_STAGE=$(mktemp "$(dirname -- "$UNIT_FILE")/hermes-gateway.stage.XXXXXX.service")
REBOOT_HELPER_STAGE=$(mktemp "$(dirname -- "$REBOOT_HELPER_DISK")/hermes-reboot-helper.stage.XXXXXX")
REBOOT_SERVICE_STAGE=$(mktemp "$(dirname -- "$REBOOT_SERVICE_FILE")/hermes-reboot-helper.stage.XXXXXX.service")
REBOOT_PATH_STAGE=$(mktemp "$(dirname -- "$REBOOT_PATH_FILE")/hermes-reboot-helper.stage.XXXXXX.path")
REBOOT_HELPER_BACKUP=$REBOOT_HELPER_DISK.backup.$$
REBOOT_SERVICE_BACKUP=$REBOOT_SERVICE_FILE.backup.$$
REBOOT_PATH_BACKUP=$REBOOT_PATH_FILE.backup.$$
REBOOT_HELPER_SWAPPED=0
REBOOT_SERVICE_SWAPPED=0
REBOOT_PATH_SWAPPED=0
REBOOT_HELPER_BACKED_UP=0
REBOOT_SERVICE_BACKED_UP=0
REBOOT_PATH_BACKED_UP=0
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
        if [ "$PLUGIN_SWAPPED" = 1 ]; then runuser -u hermes -- rm -rf -- "$PLUGIN_DEST"; fi
        if [ "$PLUGIN_BACKED_UP" = 1 ]; then
            runuser -u hermes -- sh -c 'test ! -e "$1" || mv -- "$1" "$2"' sh "$PLUGIN_BACKUP" "$PLUGIN_DEST"
        fi
        systemctl daemon-reload >/dev/null 2>&1 || true
        [ "$WAS_ENABLED" = 0 ] || systemctl enable hermes-gateway >/dev/null 2>&1 || true
        [ "$WAS_ACTIVE" = 0 ] || systemctl start hermes-gateway >/dev/null 2>&1 || true
    fi
    if [ "$REBOOT_HELPER_SWAPPED" = 1 ]; then rm -f -- "$REBOOT_HELPER_DISK"; fi
    if [ "$REBOOT_SERVICE_SWAPPED" = 1 ]; then rm -f -- "$REBOOT_SERVICE_FILE"; fi
    if [ "$REBOOT_PATH_SWAPPED" = 1 ]; then rm -f -- "$REBOOT_PATH_FILE"; fi
    if [ "$REBOOT_HELPER_BACKED_UP" = 1 ] && [ -e "$REBOOT_HELPER_BACKUP" ]; then mv -- "$REBOOT_HELPER_BACKUP" "$REBOOT_HELPER_DISK"; fi
    if [ "$REBOOT_SERVICE_BACKED_UP" = 1 ] && [ -e "$REBOOT_SERVICE_BACKUP" ]; then mv -- "$REBOOT_SERVICE_BACKUP" "$REBOOT_SERVICE_FILE"; fi
    if [ "$REBOOT_PATH_BACKED_UP" = 1 ] && [ -e "$REBOOT_PATH_BACKUP" ]; then mv -- "$REBOOT_PATH_BACKUP" "$REBOOT_PATH_FILE"; fi
    rm -rf -- "$INSTALLER_TMP" "$PLUGIN_SOURCE_STAGE" "$UNIT_STAGE" "$REBOOT_HELPER_STAGE" "$REBOOT_SERVICE_STAGE" "$REBOOT_PATH_STAGE"
    runuser -u hermes -- rm -rf -- "$PLUGIN_STAGE"
    exit "$status"
}
trap rollback EXIT

repair_agent_tree() {
    directory=$1
    [ -e "$directory" ] || return 0
    [ -d "$directory" ] && [ ! -L "$directory" ] || return 1

    unsafe_entry=$(find -P "$directory" -xdev \( -type l -o \( ! -type d ! -type f \) \) -print -quit) || return 1
    [ -z "$unsafe_entry" ] || return 1
    chown -R --no-dereference hermes:hermes "$directory"
}

validate_runtime() {
    case "$HERMES_BIN_DISK:$HERMES_PYTHON_DISK:$UV_BIN_DISK" in /var/lib/hermes/*) : ;; *) return 1 ;; esac
    runuser -u hermes -- sh -c '
        for executable in "$1" "$2" "$3"; do
            if test -L "$executable"; then
                resolved=$(readlink -f -- "$executable") || exit 1
                case "$resolved" in "$5"/*) : ;; *) exit 1 ;; esac
                test -f "$resolved" && test ! -L "$resolved" || exit 1
                test "$(stat -c %U:%G "$resolved")" = hermes:hermes || exit 1
            else
                test -f "$executable" && test -x "$executable" || exit 1
            fi
        done
        test -z "$(find "$4" -xdev ! -user hermes -print -quit)" || exit 1
        "$2" -c "import sys; raise SystemExit(0 if sys.prefix != sys.base_prefix else 1)"
        "$1" --version >/dev/null
        "$3" --version >/dev/null
    ' sh "$HERMES_BIN_DISK" "$HERMES_PYTHON_DISK" "$UV_BIN_DISK" "$HERMES_HOME_DISK" "$HERMES_ACCOUNT_HOME_DISK"
}

install_root_asset() {
    source=$1
    destination=$2
    mode=$3
    if [ -e "$destination" ] || [ -L "$destination" ]; then
        [ -f "$destination" ] && [ ! -L "$destination" ] || return 1
        [ "$(stat -c %U:%G "$destination")" = root:root ] || return 1
    fi
    install -o root -g root -m "$mode" -- "$source" "$destination"
    [ -f "$destination" ] && [ ! -L "$destination" ] && [ "$(stat -c %U:%G:%a "$destination")" = "root:root:$mode" ]
}

backup_root_asset() {
    source=$1
    backup=$2
    backed_up=$3
    if [ -e "$source" ] || [ -L "$source" ]; then
        [ -f "$source" ] && [ ! -L "$source" ] || return 1
        [ "$(stat -c %U:%G "$source")" = root:root ] || return 1
        mv -- "$source" "$backup"
        printf -v "$backed_up" '%s' 1
    fi
}

# Runtime installation is separate and idempotent. Docker access is granted only after validation.
repair_agent_tree "$HERMES_AGENT_DISK" || {
    printf '%s\n' 'Error: unsafe Hermes Agent directory.' >&2
    exit 1
}
if ! validate_runtime; then
    printf '%s\n' 'Installing Hermes runtime (Python and uv)...' >&2
    curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location "$OFFICIAL_INSTALLER_URL" --output "$INSTALLER_TMP"
    printf '%s  %s\n' "$INSTALLER_SHA256" "$INSTALLER_TMP" | sha256sum --check --status
    chown hermes:hermes "$INSTALLER_TMP"
    chmod 0700 "$INSTALLER_TMP"
    runuser -u hermes -- env HOME="$(path_at /var/lib/hermes)" HERMES_HOME="$HERMES_HOME_DISK" \
        UV_HTTP_TIMEOUT=30 UV_HTTP_RETRIES=2 \
        bash -c 'cd "$1" && exec bash "$2" "${@:3}"' bash "$(path_at /var/lib/hermes)" "$INSTALLER_TMP" \
        --skip-setup --skip-browser --dir "$HERMES_AGENT_DISK" \
        --hermes-home "$HERMES_HOME_DISK" --commit "$HERMES_COMMIT"
    validate_runtime || { printf '%s\n' 'Ошибка: установленная среда Hermes не прошла проверку.' >&2; exit 1; }
fi
if [ -e "$MANAGED_ENV_FILE" ] || [ -L "$MANAGED_ENV_FILE" ]; then
    [ -f "$MANAGED_ENV_FILE" ] && [ ! -L "$MANAGED_ENV_FILE" ] || {
        printf '%s\n' 'Error: /etc/hermes/.env must be a regular file.' >&2
        exit 1
    }
    chown root:hermes "$MANAGED_ENV_FILE"
    chmod 0640 "$MANAGED_ENV_FILE"
fi
printf '%s\n' 'Installing XMPP Python dependencies...' >&2
runuser -u hermes -- env HOME="$HERMES_ACCOUNT_HOME_DISK" HERMES_HOME="$HERMES_HOME_DISK" \
    UV_HTTP_TIMEOUT=30 UV_HTTP_RETRIES=2 \
    bash -c 'cd "$1" && exec "$2" pip install --python "$3" "slixmpp>=1.12,<2" "slixmpp-omemo==2.2.0" "aiohttp>=3.9,<4" pytest' \
    bash "$HERMES_ACCOUNT_HOME_DISK" "$UV_BIN_DISK" "$HERMES_PYTHON_DISK"

# Stage the complete allowlisted plugin before stopping the service.
cp -- "$PLUGIN_SOURCE/__init__.py" "$PLUGIN_SOURCE/adapter.py" "$PLUGIN_SOURCE/plugin.yaml" "$PLUGIN_SOURCE_STAGE/"
mkdir -p -- "$PLUGIN_SOURCE_STAGE/xmpp_bridge"
while IFS= read -r source_file; do cp -- "$source_file" "$PLUGIN_SOURCE_STAGE/xmpp_bridge/"; done < <(find "$PLUGIN_SOURCE/xmpp_bridge" -maxdepth 1 -type f -name '*.py' -print)
mkdir -p -- "$PLUGIN_SOURCE_STAGE/xmpp_image_gen"
cp -- "$PLUGIN_SOURCE/xmpp_image_gen/__init__.py" "$PLUGIN_SOURCE_STAGE/xmpp_image_gen/"
chown -R hermes:hermes "$PLUGIN_SOURCE_STAGE"
chmod -R go-rwx "$PLUGIN_SOURCE_STAGE"
runuser -u hermes -- sh -c '
    cp -- "$1/__init__.py" "$1/adapter.py" "$1/plugin.yaml" "$2/"
    mkdir -p -- "$2/xmpp_bridge"
    for source_file in "$1"/xmpp_bridge/*.py; do
        test -f "$source_file" && test ! -L "$source_file" || exit 1
        cp -- "$source_file" "$2/xmpp_bridge/"
    done
    mkdir -p -- "$2/xmpp_image_gen"
    test -f "$1/xmpp_image_gen/__init__.py" && test ! -L "$1/xmpp_image_gen/__init__.py" || exit 1
    cp -- "$1/xmpp_image_gen/__init__.py" "$2/xmpp_image_gen/"
    test -f "$2/xmpp_bridge/__init__.py" && test ! -L "$2/xmpp_bridge/__init__.py" || exit 1
    chmod -R go-rwx "$2"
' sh "$PLUGIN_SOURCE_STAGE" "$PLUGIN_STAGE" || { printf '%s\n' 'Ошибка: не удалось подготовить плагин XMPP.' >&2; exit 1; }

runuser -u hermes -- "$HERMES_PYTHON_DISK" -c '
import sys
from pathlib import Path
import yaml

path = Path(sys.argv[1])
data = yaml.safe_load(path.read_text("utf-8")) if path.exists() else {}
if not isinstance(data, dict):
    raise SystemExit("Hermes config.yaml must contain a mapping")
plugins = data.setdefault("plugins", {})
if not isinstance(plugins, dict):
    raise SystemExit("Hermes plugins config must contain a mapping")
enabled = plugins.setdefault("enabled", [])
if not isinstance(enabled, list):
    raise SystemExit("Hermes plugins.enabled must be a list")
if "xmpp-platform" not in enabled:
    enabled.append("xmpp-platform")
tmp = path.with_suffix(".yaml.tmp")
tmp.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
tmp.replace(path)
' "$PLUGIN_CONFIG_FILE" || { printf '%s\n' 'Error: could not enable the XMPP plugin.' >&2; exit 1; }

sed "s|^ExecStart=.*|ExecStart=$HERMES_BIN gateway run|" "$SCRIPT_DIR/hermes-gateway.service" >"$UNIT_STAGE"
chmod 0644 "$UNIT_STAGE"
cp -- "$SCRIPT_DIR/hermes-reboot-helper.sh" "$REBOOT_HELPER_STAGE"
cp -- "$SCRIPT_DIR/hermes-reboot-helper.service" "$REBOOT_SERVICE_STAGE"
cp -- "$SCRIPT_DIR/hermes-reboot-helper.path" "$REBOOT_PATH_STAGE"
chmod 0755 "$REBOOT_HELPER_STAGE"
chmod 0644 "$REBOOT_SERVICE_STAGE" "$REBOOT_PATH_STAGE"
backup_root_asset "$REBOOT_HELPER_DISK" "$REBOOT_HELPER_BACKUP" REBOOT_HELPER_BACKED_UP
install_root_asset "$REBOOT_HELPER_STAGE" "$REBOOT_HELPER_DISK" 755
REBOOT_HELPER_SWAPPED=1
systemd-analyze verify "$UNIT_STAGE" "$REBOOT_SERVICE_STAGE" "$REBOOT_PATH_STAGE"

# Preserve env content, reject unsafe file types, and enforce manager-only secrets.
if [ -e "$ENV_FILE" ] || [ -L "$ENV_FILE" ]; then
    [ -f "$ENV_FILE" ] && [ ! -L "$ENV_FILE" ] || { printf '%s\n' 'Ошибка: hermes.env должен быть регулярным файлом.' >&2; exit 1; }
elif [ "${HERMES_DEFER_SERVICE_START:-0}" != 1 ]; then
    cp -- "$SCRIPT_DIR/hermes.env.example" "$ENV_FILE"
fi
if [ -e "$ENV_FILE" ]; then
    chown root:root "$ENV_FILE"
    chmod 0600 "$ENV_FILE"
fi

usermod -aG docker hermes

systemctl is-active --quiet hermes-gateway && WAS_ACTIVE=1 || true
systemctl is-enabled --quiet hermes-gateway && WAS_ENABLED=1 || true
TRANSACTION=1
if [ "$WAS_ACTIVE" = 1 ]; then systemctl stop hermes-gateway; fi

if runuser -u hermes -- sh -c 'test -e "$1"' sh "$PLUGIN_DEST"; then
    runuser -u hermes -- mv -- "$PLUGIN_DEST" "$PLUGIN_BACKUP"
    PLUGIN_BACKED_UP=1
fi
runuser -u hermes -- mv -- "$PLUGIN_STAGE" "$PLUGIN_DEST"
PLUGIN_SWAPPED=1
if [ -e "$UNIT_FILE" ]; then mv -- "$UNIT_FILE" "$UNIT_BACKUP"; UNIT_BACKED_UP=1; fi
mv -- "$UNIT_STAGE" "$UNIT_FILE"
UNIT_SWAPPED=1
chown root:root "$UNIT_FILE"
runuser -u hermes -- chmod -R go-rwx "$PLUGIN_DEST"
chmod 0644 "$UNIT_FILE"
backup_root_asset "$REBOOT_SERVICE_FILE" "$REBOOT_SERVICE_BACKUP" REBOOT_SERVICE_BACKED_UP
install_root_asset "$REBOOT_SERVICE_STAGE" "$REBOOT_SERVICE_FILE" 644
REBOOT_SERVICE_SWAPPED=1
backup_root_asset "$REBOOT_PATH_FILE" "$REBOOT_PATH_BACKUP" REBOOT_PATH_BACKED_UP
install_root_asset "$REBOOT_PATH_STAGE" "$REBOOT_PATH_FILE" 644
REBOOT_PATH_SWAPPED=1
systemctl daemon-reload
systemctl enable --now hermes-reboot-helper.path
if [ "$WAS_ENABLED" = 1 ]; then systemctl disable hermes-gateway; fi

if ! systemctl cat hermes-gateway >/dev/null; then
    printf '%s\n' 'Error: Hermes service unit is not loaded.' >&2
    exit 1
fi
if [ "${HERMES_DEFER_SERVICE_START:-0}" = 1 ] && [ ! -e "$ENV_FILE" ]; then
    printf '%s\n' 'Пропуск doctor: конфигурация будет создана основным установщиком.' >&2
elif ! runuser -u hermes -- env HOME="$HERMES_ACCOUNT_HOME_DISK" HERMES_HOME="$HERMES_HOME_DISK" \
    bash -c 'cd "$1" && exec "$2" doctor' bash "$HERMES_ACCOUNT_HOME_DISK" "$HERMES_BIN_DISK" >/dev/null; then
    printf '%s\n' 'Error: Hermes diagnostic check failed.' >&2
    exit 1
fi

runuser -u hermes -- rm -rf -- "$PLUGIN_BACKUP"
rm -f -- "$UNIT_BACKUP"
rm -f -- "$REBOOT_HELPER_BACKUP" "$REBOOT_SERVICE_BACKUP" "$REBOOT_PATH_BACKUP"
TRANSACTION=0
if [ "${HERMES_DEFER_SERVICE_START:-0}" != 1 ] && confirm_service_start; then
    if ! systemctl enable --now hermes-gateway \
        || ! systemctl is-enabled --quiet hermes-gateway \
        || ! systemctl is-active --quiet hermes-gateway; then
        printf '%s\n' 'Error: Hermes service did not start successfully.' >&2
        service_diagnostics
        exit 1
    fi
    printf 'Hermes %s installed and running.\n' "$HERMES_RELEASE"
else
    printf 'Hermes %s installed. Service remains stopped and disabled.\n' "$HERMES_RELEASE"
fi
