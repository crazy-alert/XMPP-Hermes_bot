#!/usr/bin/env bash
set -Eeuo pipefail

REPOSITORY=https://github.com/crazy-alert/XMPP-Hermes_bot.git
REF=${HERMES_INSTALL_REF:-main}
STAGE=''
PASSWORD=''

cleanup() {
    PASSWORD=''
    [ -z "$STAGE" ] || rm -rf -- "$STAGE"
}
trap cleanup EXIT HUP INT TERM

fail() {
    printf 'Error: %s\n' "$1" >&2
    exit 1
}

INPUT_FD=0
if [ ! -t 0 ] && [ -t 1 ] && [ -r /dev/tty ]; then
    if exec 3</dev/tty 2>/dev/null; then
        INPUT_FD=3
    fi
fi

read_line() {
    local destination=$1
    IFS= read -r "$destination" <&"$INPUT_FD"
}

read_secret() {
    local destination=$1
    IFS= read -r -s "$destination" <&"$INPUT_FD"
}

usage() {
    printf 'Usage: %s [--ref <branch-or-tag>]\n' "$0" >&2
    exit 2
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --ref) [ "$#" -ge 2 ] || usage; REF=$2; shift 2 ;;
        *) usage ;;
    esac
done

[ "$(id -u)" -eq 0 ] || fail 'run this installer as root'
[[ "$REF" =~ ^[A-Za-z0-9._/-]+$ && "$REF" != *..* && "$REF" != /* && "$REF" != */ ]] || fail 'invalid Git branch or tag'
. /etc/os-release
[ "${ID:-}" = ubuntu ] && dpkg --compare-versions "${VERSION_ID:-0}" ge 24.04 || fail 'Ubuntu 24.04 or newer is required'

docker_ready() {
    command -v docker >/dev/null 2>&1 && getent group docker >/dev/null 2>&1 && docker info >/dev/null 2>&1
}

confirm_docker_installation() {
    [ -t "$INPUT_FD" ] || fail 'Docker Engine is unavailable and installation needs interactive confirmation'
    printf '%s' 'Docker Engine is not ready. Install Ubuntu package docker.io? [y/N] '
    read_line answer || fail 'installation cancelled'
    case "${answer%$'\r'}" in
        y|Y|yes|YES|Yes|д|Д|да|ДА|Да) ;;
        *) fail 'Docker installation declined' ;;
    esac
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y --no-install-recommends docker.io
    systemctl enable --now docker
    docker_ready || fail 'Docker Engine failed the post-installation check'
}

docker_ready || confirm_docker_installation

apt-get update
apt-get install -y --no-install-recommends ca-certificates git
STAGE=$(mktemp -d /tmp/hermes-xmpp.XXXXXX)
git init -q "$STAGE"
git -C "$STAGE" remote add origin "$REPOSITORY"
git -C "$STAGE" fetch --depth=1 origin "$REF"
git -C "$STAGE" checkout -q --detach FETCH_HEAD
EXPECTED_COMMIT=$(git -C "$STAGE" rev-parse FETCH_HEAD) || fail 'could not resolve the downloaded release'
[ "$(git -C "$STAGE" rev-parse HEAD)" = "$EXPECTED_COMMIT" ] || fail 'checked out commit does not match the requested ref'
[ -f "$STAGE/deploy/install-on-ubuntu.sh" ] || fail 'verified checkout has no deployment installer'

HERMES_DEFER_SERVICE_START=1 bash "$STAGE/deploy/install-on-ubuntu.sh"
systemctl disable --now hermes-gateway

read_value() {
    local prompt=$1 destination=$2 value
    printf '%s' "$prompt" >&2
    read_line value || fail 'configuration cancelled'
    value=${value%$'\r'}
    printf -v "$destination" '%s' "$value"
}

validate_host() {
    local value=$1 label
    [ "${#value}" -le 253 ] || return 1
    [[ "$value" =~ ^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$ ]] || return 1
    IFS=. read -r -a labels <<<"$value"
    for label in "${labels[@]}"; do [ "${#label}" -le 63 ] || return 1; done
}

validate_port() {
    [[ "$1" =~ ^[0-9]{1,5}$ ]] && [ "$1" -ge 1 ] && [ "$1" -le 65535 ]
}

validate_full_jid() {
    case "$1" in *['"\\']*) return 1 ;; esac
    [[ ! "$1" =~ [[:cntrl:]] ]] || return 1
    [ "${#1}" -le 3071 ] && [[ "$1" =~ ^[^@/:[:space:]]+@[^@/:[:space:]]+/[^/[:space:]]+$ ]]
}

validate_bare_jid() {
    case "$1" in *['"\\']*) return 1 ;; esac
    [[ ! "$1" =~ [[:cntrl:]] ]] || return 1
    [ "${#1}" -le 3071 ] && [[ "$1" =~ ^[^@/:[:space:]]+@[^@/:[:space:]]+$ ]]
}

validate_nick() {
    [ -n "$1" ] && [ "${#1}" -le 64 ] && [[ ! "$1" =~ [[:cntrl:]] ]]
}

validate_password() {
    [ -n "$1" ] && [ "${#1}" -le 1024 ] && [[ ! "$1" =~ [[:space:][:cntrl:]] ]]
}

quote_env() {
    local value=$1
    value=${value//\\/\\\\}
    value=${value//\"/\\\"}
    value=${value//\$/\\\$}
    value=${value//\`/\\\`}
    printf '"%s"' "$value"
}

write_xmpp_env() {
    local env_tmp
    env_tmp=$(mktemp "$ENV_DIR/.hermes.env.retry.XXXXXX")
    chmod 0600 "$env_tmp"
    printf 'HERMES_HOME=/var/lib/hermes/.hermes\nXMPP_JID=%s\nXMPP_ALLOWED_USERS=%s\nXMPP_NICK=%s\nXMPP_STATE_PATH=/var/lib/hermes/.hermes/xmpp/rooms.json\nXMPP_HOST=%s\nXMPP_PORT=%s\nXMPP_TLS_MODE=%s\nXMPP_ADMIN_STATE_PATH=/var/lib/hermes/.hermes/xmpp/admin.json\nXMPP_PASSWORD=%s\n' \
        "$(quote_env "$JID")" "$(quote_env "$OWNER")" "$(quote_env "$NICK")" "$(quote_env "$HOST")" "$PORT" "$TLS_MODE" "$(quote_env "$PASSWORD")" >"$env_tmp"
    chown root:root "$env_tmp"
    sync -f "$env_tmp"
    mv -f -- "$env_tmp" "$ENV_FILE"
    sync -f "$ENV_FILE"
    sync_dir "$ENV_DIR"
}

read_value 'XMPP host: ' HOST
validate_host "$HOST" || fail 'invalid XMPP host'
read_value 'XMPP port: ' PORT
validate_port "$PORT" || fail 'invalid XMPP port'
read_value 'XMPP TLS mode (direct): ' TLS_MODE
[ "$TLS_MODE" = direct ] || fail 'only direct XMPP TLS is supported'
read_value 'Bot full JID with resource: ' JID
validate_full_jid "$JID" || fail 'invalid bot JID'
read_value 'Bot nick: ' NICK
validate_nick "$NICK" || fail 'invalid bot nick'
printf '%s' 'XMPP password: ' >&2
read_secret PASSWORD || fail 'configuration cancelled'
PASSWORD=${PASSWORD%$'\r'}
printf '\n'
validate_password "$PASSWORD" || fail 'invalid XMPP password'
read_value 'First owner bare JID: ' OWNER
validate_bare_jid "$OWNER" || fail 'invalid owner JID'

ENV_DIR=/etc/hermes
ENV_FILE=$ENV_DIR/hermes.env
STATE_DIR=/var/lib/hermes/.hermes/xmpp
STATE_FILE=$STATE_DIR/admin.json
[ -d "$ENV_DIR" ] && [ ! -L "$ENV_DIR" ] || fail 'unsafe environment directory'
[ ! -e "$ENV_FILE" ] || { [ -f "$ENV_FILE" ] && [ ! -L "$ENV_FILE" ]; } || fail 'unsafe environment file'
runuser -u hermes -- mkdir -p -- "$STATE_DIR"
runuser -u hermes -- sh -c '[ ! -e "$1" ] || { test -f "$1" && test ! -L "$1"; }' sh "$STATE_FILE" || fail 'unsafe admin-state file'

exec 9>"$ENV_DIR/.xmpp-config.lock"
flock -x 9
chmod 0600 "$ENV_DIR/.xmpp-config.lock"
TXN_DIR=$ENV_DIR/.xmpp-config-transaction

sync_dir() {
    sync -f "$1"
}

restore_previous_generation() {
    local env_restore_tmp state_restore_tmp
    if [ -e "$TXN_DIR/env.present" ]; then
        env_restore_tmp=$(mktemp "$ENV_DIR/.hermes.env.restore.XXXXXX")
        cp -p -- "$TXN_DIR/env.old" "$env_restore_tmp"
        chmod 0600 "$env_restore_tmp"
        chown root:root "$env_restore_tmp"
        sync -f "$env_restore_tmp"
        mv -f -- "$env_restore_tmp" "$ENV_FILE"
        sync -f "$ENV_FILE"
    else
        rm -f -- "$ENV_FILE"
    fi
    sync_dir "$ENV_DIR"
    if [ -e "$TXN_DIR/state.present" ]; then
        state_restore_tmp=$(mktemp "$STATE_DIR/.admin.json.restore.XXXXXX")
        cp -p -- "$TXN_DIR/state.old" "$state_restore_tmp"
        chown hermes:hermes "$state_restore_tmp"
        chmod 0600 "$state_restore_tmp"
        sync -f "$state_restore_tmp"
        runuser -u hermes -- mv -f -- "$state_restore_tmp" "$STATE_FILE"
        sync -f "$STATE_FILE"
    else
        runuser -u hermes -- rm -f -- "$STATE_FILE"
    fi
    sync_dir "$STATE_DIR"
}

clear_transaction() {
    rm -rf -- "$TXN_DIR"
    sync_dir "$ENV_DIR"
}

if [ -d "$TXN_DIR" ]; then
    [ ! -L "$TXN_DIR" ] || fail 'unsafe configuration transaction marker'
    if [ -e "$TXN_DIR/.commit" ]; then
        clear_transaction
    elif [ -e "$TXN_DIR/backups.ready" ]; then
        restore_previous_generation
        clear_transaction
    else
        clear_transaction
    fi
fi
mkdir -- "$TXN_DIR"
chmod 0700 "$TXN_DIR"
sync_dir "$ENV_DIR"
transaction_cleanup() {
    if [ -e "$TXN_DIR/backups.ready" ] && [ ! -e "$TXN_DIR/.commit" ]; then
        restore_previous_generation
    fi
    clear_transaction
    cleanup
}
trap 'exit 1' HUP INT TERM
trap transaction_cleanup EXIT
if [ -f "$ENV_FILE" ]; then
    cp -p -- "$ENV_FILE" "$TXN_DIR/env.old"
    sync -f "$TXN_DIR/env.old"
    touch "$TXN_DIR/env.present"
    sync -f "$TXN_DIR/env.present"
fi
if [ -f "$STATE_FILE" ]; then
    cp -p -- "$STATE_FILE" "$TXN_DIR/state.old"
    sync -f "$TXN_DIR/state.old"
    touch "$TXN_DIR/state.present"
    sync -f "$TXN_DIR/state.present"
fi
touch "$TXN_DIR/backups.ready"
sync -f "$TXN_DIR/backups.ready"
sync_dir "$TXN_DIR"
ENV_TMP=$(mktemp "$TXN_DIR/hermes.env.XXXXXX")
STATE_TMP=$(runuser -u hermes -- mktemp "$STATE_DIR/.admin.json.XXXXXX")
chmod 0600 "$ENV_TMP"
printf 'HERMES_HOME=/var/lib/hermes/.hermes\nXMPP_JID=%s\nXMPP_ALLOWED_USERS=%s\nXMPP_NICK=%s\nXMPP_STATE_PATH=/var/lib/hermes/.hermes/xmpp/rooms.json\nXMPP_HOST=%s\nXMPP_PORT=%s\nXMPP_TLS_MODE=%s\nXMPP_ADMIN_STATE_PATH=/var/lib/hermes/.hermes/xmpp/admin.json\nXMPP_PASSWORD=%s\n' \
    "$(quote_env "$JID")" "$(quote_env "$OWNER")" "$(quote_env "$NICK")" "$(quote_env "$HOST")" "$PORT" "$TLS_MODE" "$(quote_env "$PASSWORD")" >"$ENV_TMP"
runuser -u hermes -- sh -c 'umask 077; printf "{\\\"version\\\":1,\\\"revision\\\":0,\\\"owners\\\":[\\\"%s\\\"],\\\"trusted_jids\\\":[],\\\"model\\\":null,\\\"endpoint\\\":null,\\\"token\\\":null}\\n" "$2" >"$1"; sync -f "$1"' sh "$STATE_TMP" "$OWNER"
sync -f "$ENV_TMP"
mv -f "$ENV_TMP" "$ENV_FILE"
sync_dir "$ENV_DIR"
runuser -u hermes -- mv -f "$STATE_TMP" "$STATE_FILE"
sync_dir "$STATE_DIR"
chmod 0600 "$ENV_FILE"
chown root:root "$ENV_FILE"
sync -f "$ENV_FILE"
sync_dir "$ENV_DIR"
PASSWORD=''
touch "$TXN_DIR/.commit"
sync -f "$TXN_DIR/.commit"
sync_dir "$TXN_DIR"

printf '%s' 'Start Hermes service now? [y/N] ' >&2
read_line answer || answer=''
case "${answer%$'\r'}" in
    y|Y|yes|YES|Yes)
        started=0
        for attempt in 1 2 3; do
            if systemctl enable --now hermes-gateway \
                && systemctl is-active --quiet hermes-gateway; then
                started=1
                break
            fi
            systemctl status --no-pager hermes-gateway >&2 || true
            journalctl -u hermes-gateway --no-pager -n 30 >&2 || true
            [ "$attempt" -lt 3 ] || break
            printf '%s' 'JID или пароль могут быть неверными. Ввести полный JID и пароль ещё раз? [y/N] ' >&2
            read_line retry || retry=''
            case "${retry%$'\r'}" in y|Y|yes|YES|Yes|РґР°|Рґ) ;; *) break ;; esac
            read_value 'Bot full JID with resource: ' JID
            validate_full_jid "$JID" || { printf '%s\n' 'Некорректный JID.' >&2; continue; }
            printf '%s' 'XMPP password: ' >&2
            read_secret PASSWORD || PASSWORD=''
            PASSWORD=${PASSWORD%$'\r'}
            printf '\n' >&2
            validate_password "$PASSWORD" || { printf '%s\n' 'Некорректный пароль.' >&2; continue; }
            write_xmpp_env
        done
        [ "$started" = 1 ] || fail 'Hermes не запустился; проверьте JID, пароль и журнал службы'
        ;;
    *) printf '%s\n' 'Hermes is installed and remains stopped.' ;;
esac
