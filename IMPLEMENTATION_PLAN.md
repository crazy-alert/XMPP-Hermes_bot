# Hermes XMPP Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Установить Hermes Agent на Ubuntu 24.04 и добавить безопасный XMPP platform plugin для личных сообщений и приватных MUC-комнат.

**Architecture:** Hermes запускается штатным gateway под непривилегированным пользователем `hermes`. Сторонний плагин `xmpp` использует Slixmpp, преобразует разрешённые XMPP-события в `MessageEvent`, хранит список приглашённых комнат в атомарном JSON-файле и использует штатные сессии Hermes.

**Tech Stack:** Python 3.11+, Hermes Agent plugin API, Slixmpp 1.12+, pytest, systemd, Docker terminal backend, ejabberd.

## Global Constraints

- VPS: Ubuntu 24.04.1 LTS x86_64.
- XMPP-домен: `example.com`; аккаунт бота: `bot@example.com`; ресурс: `Hermes`.
- Модель: `gpt-5.6-sol`; OpenAI-совместимый endpoint: `https://llm.example.com/v1`.
- Разрешённые bare JID: `admin@example.com`, `alice@example.com`, `bob@example.com`.
- В личке отвечать на каждое непустое текстовое сообщение разрешённого пользователя.
- В MUC отвечать только разрешённому пользователю при обращении к боту или XEP-0461 reply на сообщение бота.
- Принимать прямые и mediated приглашения в приватные MUC только от разрешённых JID; комнаты сохранять между перезапусками.
- Не реализовывать OMEMO, голос, файлы, публичный Hermes API и команды управления ejabberd.
- Не хранить API-ключ и XMPP-пароль в репозитории, тестах, unit-файле или журнале.
- Команды агента выполнять через Docker terminal backend без проброса секретов.

## File Structure

- `hermes-xmpp/plugin.yaml` — манифест платформы и описание переменных окружения.
- `hermes-xmpp/adapter.py` — регистрация платформы и адаптер Hermes.
- `hermes-xmpp/xmpp_bridge/policy.py` — нормализация JID, allowlist, mention/reply routing и session keys.
- `hermes-xmpp/xmpp_bridge/state.py` — атомарное постоянное хранение MUC JID.
- `hermes-xmpp/xmpp_bridge/client.py` — Slixmpp-клиент, TLS, DM/MUC события, reconnect и отправка.
- `hermes-xmpp/xmpp_bridge/models.py` — внутренние типизированные события без зависимости от Hermes.
- `tests/test_policy.py` — unit-тесты авторизации и маршрутизации.
- `tests/test_state.py` — unit-тесты persistent state.
- `tests/test_client_events.py` — тесты преобразования Slixmpp-событий.
- `tests/test_adapter.py` — контракт адаптера Hermes с подменённым клиентом.
- `deploy/hermes-gateway.service` — systemd unit.
- `deploy/hermes.env.example` — шаблон без секретов.
- `deploy/install-on-ubuntu.sh` — идемпотентная установка файлов и службы.
- `README.md` — установка, настройка, smoke tests и откат.

---

### Task 1: Каркас плагина и модели событий

**Files:**
- Create: `hermes-xmpp/plugin.yaml`
- Create: `hermes-xmpp/adapter.py`
- Create: `hermes-xmpp/xmpp_bridge/__init__.py`
- Create: `hermes-xmpp/xmpp_bridge/models.py`
- Create: `tests/test_plugin_manifest.py`

**Interfaces:**
- Produces: `InboundXmppMessage`, `XmppInvite`, `DeliveryTarget`, `register(ctx)`.
- `InboundXmppMessage` fields: `message_id`, `chat_jid`, `sender_jid`, `sender_nick`, `body`, `is_group`, `reply_to_id`.

- [ ] **Step 1: Write failing manifest and model tests**

Assert that `plugin.yaml` has `name: xmpp-platform`, `kind: platform`, requires `XMPP_JID` and `XMPP_PASSWORD`, and that immutable dataclasses preserve bare-JID event data without importing Hermes.

Run: `pytest -q tests/test_plugin_manifest.py`

Expected: FAIL because plugin files do not exist.

- [ ] **Step 2: Implement the minimal manifest and models**

`plugin.yaml` must declare `SLIXMPP>=1.12,<2`, secret fields as password inputs, and optional variables `XMPP_ALLOWED_USERS`, `XMPP_STATE_PATH`, `XMPP_HOST`, `XMPP_PORT`, `XMPP_NICK`.

`adapter.py` must expose `register(ctx)` and defer Slixmpp imports until adapter construction so `hermes gateway status` can report a missing dependency cleanly.

- [ ] **Step 3: Run focused tests**

Run: `pytest -q tests/test_plugin_manifest.py`

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add hermes-xmpp/plugin.yaml hermes-xmpp/adapter.py hermes-xmpp/xmpp_bridge tests/test_plugin_manifest.py
git commit -m "feat: add Hermes XMPP plugin skeleton"
```

### Task 2: Политика авторизации, обращений и сессий

**Files:**
- Create: `hermes-xmpp/xmpp_bridge/policy.py`
- Create: `tests/test_policy.py`

**Interfaces:**
- Produces: `normalize_bare_jid(value: str) -> str`.
- Produces: `parse_allowlist(value: str) -> frozenset[str]`.
- Produces: `route_direct(message, allowed_users) -> RoutedMessage | None`.
- Produces: `route_group(message, allowed_users, bot_jid, bot_nick, bot_message_ids) -> RoutedMessage | None`.
- Produces: `session_key(message) -> str`, with DM key `xmpp:dm:<sender>` and MUC key `xmpp:muc:<room>:<sender>`.

- [ ] **Step 1: Write failing authorization tests**

Cover case-folded bare JIDs, resource stripping, malformed/empty JIDs, permitted DM, denied DM, empty body, self-message and group messages from denied users.

Run: `pytest -q tests/test_policy.py -k authorization`

Expected: FAIL because policy functions are absent.

- [ ] **Step 2: Implement JID normalization and allowlist checks**

Use Slixmpp `JID` parsing when available; reject values without localpart or domain. Never authorize on nickname alone.

- [ ] **Step 3: Write failing mention tests**

Cover `Hermes, вопрос`, `@Hermes вопрос`, `bot@example.com вопрос`, unrelated text, substring `hermes` inside another word, self MUC nick, and XEP-0461 reply whose referenced ID is or is not in the bot-message cache.

Run: `pytest -q tests/test_policy.py -k mention`

Expected: FAIL until group routing is implemented.

- [ ] **Step 4: Implement mention stripping and session keys**

Match the actual MUC nick case-insensitively only at a token boundary. Strip only the leading mention plus spaces and `,:;—-`; reject an empty result. Treat reply as activation only when it references a recently sent bot message ID.

- [ ] **Step 5: Run all policy tests**

Run: `pytest -q tests/test_policy.py`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add hermes-xmpp/xmpp_bridge/policy.py tests/test_policy.py
git commit -m "feat: enforce XMPP authorization and mention policy"
```

### Task 3: Постоянное состояние приватных комнат

**Files:**
- Create: `hermes-xmpp/xmpp_bridge/state.py`
- Create: `tests/test_state.py`

**Interfaces:**
- Produces: `RoomState(path: Path)`.
- Produces: `RoomState.load() -> frozenset[str]`.
- Produces: `RoomState.add(room_jid: str) -> bool`.
- Produces: `RoomState.remove(room_jid: str) -> bool`.
- File schema: `{"version":1,"rooms":["room@conference.example.com"]}`.

- [ ] **Step 1: Write failing state tests**

Test missing file, canonical sorted output, duplicate add, atomic replacement, mode `0600`, corrupt JSON quarantine to `.corrupt-<UTC timestamp>`, and preservation of the last valid in-memory set after a failed write.

Run: `pytest -q tests/test_state.py`

Expected: FAIL because `RoomState` is absent.

- [ ] **Step 2: Implement atomic state storage**

Write a temporary file in the same directory, flush and `fsync`, apply `0600`, then `os.replace`. Normalize every room as a bare JID and reject non-MUC-shaped values that lack a localpart.

- [ ] **Step 3: Run state tests**

Run: `pytest -q tests/test_state.py`

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add hermes-xmpp/xmpp_bridge/state.py tests/test_state.py
git commit -m "feat: persist invited XMPP rooms safely"
```

### Task 4: Slixmpp client and event translation

**Files:**
- Create: `hermes-xmpp/xmpp_bridge/client.py`
- Create: `tests/test_client_events.py`

**Interfaces:**
- Consumes: `InboundXmppMessage`, `XmppInvite`, `RoomState`.
- Produces: `HermesXmppClient(config, on_message, on_invite)`.
- Produces async methods: `connect_and_wait()`, `disconnect()`, `join_room(room_jid)`, `send_direct(jid, body)`, `send_group(room_jid, body)`, `set_typing(jid, is_group, active)`.

- [ ] **Step 1: Write failing direct-message translation tests**

Using synthetic Slixmpp `Message` stanzas, assert correct extraction of stanza ID, bare sender, body, reply ID, and rejection of delayed MUC history and messages emitted by the bot JID.

Run: `pytest -q tests/test_client_events.py -k direct`

Expected: FAIL because client translation is absent.

- [ ] **Step 2: Implement base client and XEP registration**

Register `xep_0030`, `xep_0045`, `xep_0085`, `xep_0198`, `xep_0249`, `xep_0461`. Use normal certificate verification and `disable_starttls=False`; never set an unverified SSL context. On `session_start`, send presence, request roster, and join every stored room with `join_muc_wait(..., maxstanzas=0)`.

- [ ] **Step 3: Write failing invitation tests**

Cover mediated invite (`groupchat_invite`) and direct invite (`groupchat_direct_invite`), extracting both inviter bare JID and room bare JID without trusting message body text.

Run: `pytest -q tests/test_client_events.py -k invite`

Expected: FAIL until invitation translation is implemented.

- [ ] **Step 4: Implement invitation and MUC handling**

Call the adapter callback before persisting or joining. Do not auto-accept inside the raw stanza handler. Suppress history messages by delayed-delivery metadata and ignore the bot's actual room nick.

- [ ] **Step 5: Implement reconnect and outbound chunking**

Allow Slixmpp's reconnect loop with a bounded delay sequence 1, 2, 5, 10, 30, 60 seconds. Split outbound text at paragraph/whitespace boundaries with maximum 3500 Unicode characters and return generated stanza IDs for reply tracking.

- [ ] **Step 6: Run client tests**

Run: `pytest -q tests/test_client_events.py`

Expected: PASS without a live network.

- [ ] **Step 7: Commit**

```bash
git add hermes-xmpp/xmpp_bridge/client.py tests/test_client_events.py
git commit -m "feat: add TLS XMPP client with DM and MUC events"
```

### Task 5: Hermes platform adapter

**Files:**
- Modify: `hermes-xmpp/adapter.py`
- Create: `tests/test_adapter.py`

**Interfaces:**
- Consumes: policy functions, `RoomState`, `HermesXmppClient`.
- Produces: `XmppPlatformAdapter(BasePlatformAdapter)` implementing `connect`, `disconnect`, `send`, `send_typing`, `get_chat_info`.
- Emits: Hermes `MessageEvent` with `platform=Platform("xmpp")`, `chat_type` of `dm` or `group`, stable `chat_id`, `user_id`, `thread_id=None`, and metadata containing only nonsecret routing fields.

- [ ] **Step 1: Write failing adapter contract tests**

Mock `HermesXmppClient` and assert connect/disconnect marking, `SendResult`, DM/MUC target selection, `MessageType.TEXT`, and that forbidden messages never call `handle_message`.

Run: `pytest -q tests/test_adapter.py -k contract`

Expected: FAIL because the adapter is still a skeleton.

- [ ] **Step 2: Implement configuration validation**

Require `XMPP_JID`, `XMPP_PASSWORD`, and a nonempty `XMPP_ALLOWED_USERS`. Defaults: port 5223 only when `XMPP_HOST` is explicitly supplied for direct TLS; otherwise use SRV discovery, nick/resource `Hermes`, state path `$HERMES_HOME/xmpp/rooms.json`.

- [ ] **Step 3: Implement DM and MUC dispatch**

Convert routed messages into Hermes events. Use group identity metadata supplied by XEP-0045 when available to resolve real bare JID; if a semi-anonymous room does not expose a verifiable bare JID, deny activation rather than authorize by nick.

- [ ] **Step 4: Implement safe invitation acceptance**

Normalize inviter, check allowlist, persist room, then join. If join fails, retain the room for retry after reconnect and log only room JID plus exception class.

- [ ] **Step 5: Implement deduplication and reply tracking**

Use TTL caches bounded to 4096 entries: inbound message IDs for 10 minutes and outbound bot stanza IDs for 24 hours. Cache keys include room/chat JID to avoid cross-room collisions.

- [ ] **Step 6: Run adapter tests**

Run: `pytest -q tests/test_adapter.py`

Expected: PASS.

- [ ] **Step 7: Run the complete plugin suite**

Run: `pytest -q`

Expected: all tests PASS.

- [ ] **Step 8: Commit**

```bash
git add hermes-xmpp/adapter.py tests/test_adapter.py
git commit -m "feat: integrate XMPP routing with Hermes gateway"
```

### Task 6: Ubuntu deployment assets

**Files:**
- Create: `deploy/hermes.env.example`
- Create: `deploy/hermes-gateway.service`
- Create: `deploy/install-on-ubuntu.sh`
- Create: `README.md`
- Create: `tests/test_deploy_assets.py`

**Interfaces:**
- Consumes: completed `hermes-xmpp/` plugin.
- Produces: service command `hermes gateway run`, env file `/etc/hermes/hermes.env`, home `/var/lib/hermes`, plugin path `/var/lib/hermes/.hermes/plugins/xmpp-platform`.

- [ ] **Step 1: Write failing deployment-asset tests**

Assert that the unit has `User=hermes`, `Group=hermes`, `EnvironmentFile=/etc/hermes/hermes.env`, `NoNewPrivileges=true`, `PrivateTmp=true`, `ProtectSystem=strict`, writable paths limited to `/var/lib/hermes`, restart delay, Docker dependency, and no literal secrets. Assert example env contains the three approved JIDs.

Run: `pytest -q tests/test_deploy_assets.py`

Expected: FAIL because deployment files do not exist.

- [ ] **Step 2: Implement env template**

Include nonsecret values:

```dotenv
HERMES_HOME=/var/lib/hermes/.hermes
XMPP_JID=bot@example.com/Hermes
XMPP_ALLOWED_USERS=admin@example.com,alice@example.com,bob@example.com
XMPP_NICK=Hermes
XMPP_STATE_PATH=/var/lib/hermes/.hermes/xmpp/rooms.json
```

Represent secrets only as commented instructions to add `XMPP_PASSWORD` and the provider key produced by `hermes model`; do not include fake secret-looking values that might accidentally be deployed.

- [ ] **Step 3: Implement systemd unit**

Use absolute `ExecStart` discovered by the installer and written during installation. Grant access to Docker through group membership rather than running as root. Do not use `ProtectHome=true` because Hermes home is under `/var/lib/hermes`; use `ProtectSystem=strict`, `ReadWritePaths=/var/lib/hermes`, `UMask=0077`, `Restart=on-failure`, `RestartSec=5`.

- [ ] **Step 4: Implement idempotent installation script**

The script must:

1. require root and Ubuntu 24.04+;
2. create system user/home `hermes:/var/lib/hermes`;
3. install `curl`, `git`, CA certificates and build prerequisites;
4. run the official Hermes installer as user `hermes` with `--skip-setup`;
5. install Slixmpp and pytest into the Hermes environment using its `uv` executable;
6. copy the plugin with ownership `hermes:hermes` and mode excluding world access;
7. create `/etc/hermes/hermes.env` only when absent;
8. run `systemd-analyze verify` before enabling the unit;
9. leave the service stopped until provider and XMPP secrets are configured.

- [ ] **Step 5: Document exact server setup**

README commands must include:

```bash
docker compose exec ejabberd ejabberdctl register bot example.com
sudo -u hermes -H env HERMES_HOME=/var/lib/hermes/.hermes hermes model
sudo -u hermes -H env HERMES_HOME=/var/lib/hermes/.hermes hermes config set terminal.backend docker
sudo -u hermes -H env HERMES_HOME=/var/lib/hermes/.hermes hermes doctor
sudo systemctl enable --now hermes-gateway
sudo systemctl status hermes-gateway --no-pager
sudo journalctl -u hermes-gateway -n 100 --no-pager
```

For the password, document an interactive `read -rsp` command followed by `ejabberdctl register ... "$BOT_PASSWORD"`, then immediate `unset BOT_PASSWORD`; never put it into shell history or the README literally.

- [ ] **Step 6: Run deployment tests and shell lint**

Run: `pytest -q tests/test_deploy_assets.py && bash -n deploy/install-on-ubuntu.sh && systemd-analyze verify deploy/hermes-gateway.service`

Expected: PASS. If local systemd is unavailable, run the first two locally and the `systemd-analyze` command on Ubuntu before enabling the service.

- [ ] **Step 7: Commit**

```bash
git add deploy README.md tests/test_deploy_assets.py
git commit -m "ops: add secure Ubuntu deployment for Hermes XMPP"
```

### Task 7: VPS integration and acceptance

**Files:**
- Modify on VPS: `/etc/hermes/hermes.env`
- Modify through CLI: `/var/lib/hermes/.hermes/config.yaml`
- Verify: `/var/lib/hermes/.hermes/xmpp/rooms.json`

**Interfaces:**
- Consumes: release archive from Tasks 1-6 and two user-entered secrets.
- Produces: healthy `hermes-gateway.service` and connected `bot@example.com/Hermes` session.

- [ ] **Step 1: Capture rollback state**

Run:

```bash
sudo systemctl status hermes-gateway --no-pager || true
docker compose exec ejabberd ejabberdctl status
docker compose exec ejabberd ejabberdctl connected_users
```

Expected: ejabberd healthy; Hermes may be absent before first install.

- [ ] **Step 2: Install and configure without starting**

Run the reviewed installer, configure `hermes model` with endpoint `https://llm.example.com/v1` and model `gpt-5.6-sol`, enter the API key only in the interactive secret prompt, then edit `/etc/hermes/hermes.env` using `sudoedit` to add only `XMPP_PASSWORD` and required provider secret.

- [ ] **Step 3: Verify permissions and configuration**

Run:

```bash
sudo stat -c '%U %G %a %n' /etc/hermes/hermes.env
sudo -u hermes -H env HERMES_HOME=/var/lib/hermes/.hermes hermes doctor
sudo systemd-analyze verify /etc/systemd/system/hermes-gateway.service
```

Expected: env mode `600`, owner readable by the service, Hermes doctor has no blocking model/provider errors, unit verification exits 0.

- [ ] **Step 4: Start and verify connection**

Run:

```bash
sudo systemctl enable --now hermes-gateway
sudo systemctl is-active hermes-gateway
sudo journalctl -u hermes-gateway -n 100 --no-pager
docker compose exec ejabberd ejabberdctl connected_users | grep -F 'bot@example.com'
```

Expected: service `active`, bot appears connected, logs contain no secret values.

- [ ] **Step 5: Execute acceptance scenarios**

Manually verify all ten scenarios from the approved design: allowed/denied DM, allowed/denied invite, silent ordinary MUC text, allowed/denied mention, restart rejoin, per-user context isolation, and secret-free logs. Record only pass/fail and sanitized stanza IDs.

- [ ] **Step 6: Verify restart recovery**

Run:

```bash
sudo systemctl restart hermes-gateway
sudo systemctl is-active hermes-gateway
sudo -u hermes test -s /var/lib/hermes/.hermes/xmpp/rooms.json
sudo journalctl -u hermes-gateway --since '-2 minutes' --no-pager
```

Expected: service active and stored private rooms rejoined without new invitations.

- [ ] **Step 7: Document rollback**

Rollback command:

```bash
sudo systemctl disable --now hermes-gateway
```

This must leave ejabberd, its rooms and other accounts untouched. Deleting `bot@example.com` is a separate explicit operation and is not part of routine rollback.

- [ ] **Step 8: Final repository verification**

Run: `pytest -q && git diff --check && git status --short`

Expected: all tests pass, no whitespace errors, only intentional files remain.
