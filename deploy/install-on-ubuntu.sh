#!/usr/bin/env bash
set -Eeuo pipefail

if [ "$(id -u)" -ne 0 ]; then
    printf '%s\n' 'Ошибка: запустите установщик от root.' >&2
    exit 1
fi

if [ ! -r /etc/os-release ]; then
    printf '%s\n' 'Ошибка: не найден /etc/os-release.' >&2
    exit 1
fi

# shellcheck disable=SC1091
. /etc/os-release
if [ "${ID:-}" != "ubuntu" ] || ! dpkg --compare-versions "${VERSION_ID:-0}" ge "24.04"; then
    printf '%s\n' 'Ошибка: требуется Ubuntu 24.04 или новее.' >&2
    exit 1
fi

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"
REPO_DIR="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd -P)"
PLUGIN_SOURCE="$REPO_DIR/hermes-xmpp"
PLUGIN_DEST=/var/lib/hermes/.hermes/plugins/xmpp-platform
ENV_FILE=/etc/hermes/hermes.env
UNIT_FILE=/etc/systemd/system/hermes-gateway.service
OFFICIAL_INSTALLER_URL=https://hermes-agent.nousresearch.com/install.sh

if [ ! -f "$PLUGIN_SOURCE/plugin.yaml" ]; then
    printf 'Ошибка: плагин не найден: %s\n' "$PLUGIN_SOURCE" >&2
    exit 1
fi
if find "$PLUGIN_SOURCE" -type l -print -quit | grep -q .; then
    printf '%s\n' 'Ошибка: дерево плагина не должно содержать символические ссылки.' >&2
    exit 1
fi
if ! getent group docker >/dev/null; then
    printf '%s\n' 'Ошибка: группа docker отсутствует; сначала установите Docker Engine.' >&2
    exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
    ca-certificates curl git build-essential pkg-config libssl-dev libffi-dev

if ! getent group hermes >/dev/null; then
    groupadd --system hermes
fi
if ! getent passwd hermes >/dev/null; then
    useradd --system --create-home --home-dir /var/lib/hermes \
        --gid hermes --shell /usr/sbin/nologin hermes
fi
HERMES_HOME_DIR="$(getent passwd hermes | cut -d: -f6)"
HERMES_PRIMARY_GROUP="$(id -gn hermes)"
if [ "$HERMES_HOME_DIR" != "/var/lib/hermes" ] || [ "$HERMES_PRIMARY_GROUP" != "hermes" ]; then
    printf 'Ошибка: ожидаются hermes:hermes и home /var/lib/hermes; получены %s:%s\n' \
        hermes "$HERMES_PRIMARY_GROUP" >&2
    exit 1
fi
install -d -o hermes -g hermes -m 0700 /var/lib/hermes
usermod -aG docker hermes

INSTALLER_TMP="$(mktemp /tmp/hermes-agent-install.XXXXXX)"
UNIT_TMP="$(mktemp /tmp/hermes-gateway.XXXXXX.service)"
cleanup() {
    rm -f -- "$INSTALLER_TMP" "$UNIT_TMP"
}
trap cleanup EXIT

curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location \
    "$OFFICIAL_INSTALLER_URL" --output "$INSTALLER_TMP"
chmod 0700 "$INSTALLER_TMP"
chown hermes:hermes "$INSTALLER_TMP"
runuser -u hermes -- env \
    HOME=/var/lib/hermes HERMES_HOME=/var/lib/hermes/.hermes \
    bash "$INSTALLER_TMP" --skip-setup

HERMES_BIN="$(runuser -u hermes -- env \
    HOME=/var/lib/hermes HERMES_HOME=/var/lib/hermes/.hermes \
    PATH=/var/lib/hermes/.local/bin:/var/lib/hermes/.hermes/bin:/usr/local/bin:/usr/bin:/bin \
    sh -c 'command -v hermes')"
UV_BIN="$(runuser -u hermes -- env \
    HOME=/var/lib/hermes HERMES_HOME=/var/lib/hermes/.hermes \
    PATH=/var/lib/hermes/.hermes/bin:/var/lib/hermes/.local/bin:/usr/local/bin:/usr/bin:/bin \
    sh -c 'command -v uv')"
HERMES_BIN="$(readlink -f -- "$HERMES_BIN")"
UV_BIN="$(readlink -f -- "$UV_BIN")"
if [ ! -x "$HERMES_BIN" ] || [ ! -x "$UV_BIN" ]; then
    printf '%s\n' 'Ошибка: не удалось обнаружить исполняемые файлы Hermes и uv.' >&2
    exit 1
fi
case "$HERMES_BIN:$UV_BIN" in
    *[!A-Za-z0-9_./:+-]*)
        printf '%s\n' 'Ошибка: unsafe executable path returned by Hermes installation.' >&2
        exit 1
        ;;
esac

HERMES_PYTHON="$(head -n 1 "$HERMES_BIN" | sed 's/^#!//')"
if [ "${HERMES_PYTHON#/}" = "$HERMES_PYTHON" ] || [ ! -x "$HERMES_PYTHON" ]; then
    printf '%s\n' 'Ошибка: Hermes не указывает на абсолютный Python из своей среды.' >&2
    exit 1
fi
runuser -u hermes -- "$UV_BIN" pip install \
    --python "$HERMES_PYTHON" 'slixmpp>=1.12,<2' pytest

systemctl disable --now hermes-gateway 2>/dev/null || true
install -d -o hermes -g hermes -m 0700 "$(dirname -- "$PLUGIN_DEST")"
rm -rf -- "$PLUGIN_DEST"
cp -a -- "$PLUGIN_SOURCE" "$PLUGIN_DEST"
chown -R hermes:hermes "$PLUGIN_DEST"
chmod -R go-rwx "$PLUGIN_DEST"

install -d -o root -g hermes -m 0750 /etc/hermes
if [ ! -e "$ENV_FILE" ]; then
    install -o hermes -g hermes -m 0600 "$SCRIPT_DIR/hermes.env.example" "$ENV_FILE"
fi

sed "s|^ExecStart=.*|ExecStart=$HERMES_BIN gateway run|" \
    "$SCRIPT_DIR/hermes-gateway.service" >"$UNIT_TMP"
chmod 0644 "$UNIT_TMP"
systemd-analyze verify "$UNIT_TMP"
install -o root -g root -m 0644 "$UNIT_TMP" "$UNIT_FILE"
systemctl daemon-reload

printf '%s\n' \
    'Установка завершена. Служба остановлена и отключена.' \
    'Настройте provider и XMPP-секреты по README, затем включите службу вручную.'
