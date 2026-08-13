# XMPP Admin Commands Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Добавить работающие без AI служебные команды XMPP-бота, безопасное owner/trust/model/token управление и подтверждаемую перезагрузку хоста.

**Architecture:** Command router обрабатывает DM до Hermes adapter и работает с атомарным config store под service user. Root-привилегированная перезагрузка вынесена в отдельный узкий systemd helper; plugin создаёт только типизированный запрос после одноразового owner confirmation.

**Tech Stack:** Python 3.11+, asyncio, JSON, file locks/atomic replace, Hermes platform adapter, Slixmpp, systemd, pytest.

## Global Constraints

- Изменяющие команды: только DM от owner bare JID; MUC никогда не администрирует конфигурацию.
- `ping` case-insensitive с окружающими пробелами отвечает `pong` без AI вызова.
- API token принимается через DM по явному решению владельца; README предупреждает об отсутствии OMEMO/E2E и возможном XMPP archive/client history.
- Token не возвращается, не логируется, не входит в repr/errors; допустима только необратимая короткая маска.
- Нельзя удалить последнего owner. Trusted JID не получает admin rights.
- Отдельного room allowlist/config нет: trusted JID получает ответы в любой joined MUC при mention/reply и только при раскрытом real JID; anonymous MUC fail-closed.
- `/server reboot` требует same-owner DM confirmation с одноразовым TTL-кодом; root helper не принимает произвольные команды.
- Installer спрашивает только XMPP connection и первый owner; model/endpoint/token/trusted JID настраиваются через бот.
- Все persistent writes atomic, mode `0600`, serialized and recoverable; service remains useful for admin commands without configured provider.

---

### Task 1: Atomic admin configuration store

**Files:**
- Create: `hermes-xmpp/xmpp_bridge/admin_state.py`
- Create: `tests/test_admin_state.py`

**Interfaces:**
- Produces: immutable `AdminConfig(owners, trusted_jids, model, endpoint, token_mask, token_present)`; `AdminState.load()`, `mutate(callable)`, `set_token(secret)`; typed validation exceptions.

- [ ] Write RED tests for schema/version, first owner, normalized bare JIDs, last-owner invariant, HTTPS/loopback endpoint validation, token redaction, atomic fsync/replace, mode `0600`, corrupt-file fail-closed, concurrent mutations and interrupted replace recovery.
- [ ] Run `.venv/Scripts/python.exe -m pytest -q tests/test_admin_state.py`; expected collection/import failure.
- [ ] Implement minimal immutable state and file-lock/atomic persistence without secret-bearing repr/errors.
- [ ] Run focused plus `tests/test_state.py`; expected PASS.
- [ ] Commit `feat: add secure XMPP admin state`.

### Task 2: Owner-only command router

**Files:**
- Create: `hermes-xmpp/xmpp_bridge/commands.py`
- Create: `tests/test_commands.py`

**Interfaces:**
- Consumes: `InboundXmppMessage`, `AdminState`, sanitized status/doctor callbacks.
- Produces: `CommandResult(handled, reply, control_event)` with control events `ReloadConfig`, `RestartGateway`, `RequestHostReboot`.

- [ ] Write RED matrix for `ping`, help/status/config, model/endpoint/token, trust/owner operations, doctor/restart, unknown commands, owner/trusted/denied, DM/MUC, last-owner and redaction.
- [ ] Run focused; expected import failure.
- [ ] Implement strict shlex-free parser (command + remainder), case-insensitive command names, bounded lengths, exact validation and no secret echo/logging.
- [ ] Run focused plus policy tests; expected PASS.
- [ ] Commit `feat: add owner-only XMPP admin commands`.

### Task 3: Two-step host reboot protocol

**Files:**
- Modify: `hermes-xmpp/xmpp_bridge/commands.py`
- Create: `hermes-xmpp/xmpp_bridge/reboot.py`
- Modify: `tests/test_commands.py`
- Create: `tests/test_reboot.py`

**Interfaces:**
- Produces: cryptographically random numeric confirmation, monotonic TTL 60s, same-owner binding, one pending request, replay protection, cancel and cooldown; typed `HostRebootRequest` only.

- [ ] Write RED tests for request/confirm success, other owner, trusted/MUC, expiry, replay, cancel, concurrent request, cooldown and no code in logs.
- [ ] Implement in-memory state machine with injected clock/random source and reply-before-control-event ordering.
- [ ] Run focused tests; expected PASS.
- [ ] Commit `feat: require confirmation for host reboot`.

### Task 4: Adapter integration before Hermes

**Files:**
- Modify: `hermes-xmpp/adapter.py`
- Modify: `hermes-xmpp/xmpp_bridge/policy.py`
- Modify: `tests/test_adapter.py`
- Modify: `tests/test_policy.py`

**Interfaces:**
- Consumes: router `handle(message)` and mutable trusted snapshot.
- Produces: admin commands bypass Hermes MessageEvent; ordinary trusted messages retain current routing/session behavior; MUC policy uses trusted real JID in any joined room.

- [ ] Write RED adapter tests proving ping/admin work with failing/unconfigured Hermes, no MessageEvent, live trusted reload and all-room MUC behavior.
- [ ] Wire router before route/session dispatch and apply immutable snapshots between messages.
- [ ] Run adapter/policy/admin suites; expected PASS.
- [ ] Commit `feat: route XMPP admin commands before Hermes`.

### Task 5: Minimal installer configuration

**Files:**
- Create/Modify: `installer.sh`
- Modify: `deploy/hermes.env.example`
- Modify: `deploy/install-on-ubuntu.sh`
- Modify: `tests/test_deploy_assets.py`

**Interfaces:**
- Installer prompts only: XMPP host, port, TLS mode, bot full JID/resource, bot nick, hidden XMPP password, first owner bare JID.
- Produces protected connection env plus initialized admin state; no model/endpoint/token/trusted prompts.

- [ ] Write behavioral RED for exact prompt set, validation, hidden password, atomic env/state, abort preservation, service stopped and absence of model/token/trusted prompts.
- [ ] Implement minimal bootstrap/config handoff after verified Git checkout and transactional install.
- [ ] Run deployment tests, `bash -n`, archive LF test; expected PASS.
- [ ] Commit `feat: add minimal interactive XMPP installer`.

### Task 6: Root-owned reboot helper

**Files:**
- Create: `deploy/hermes-reboot-helper.service`
- Create: `deploy/hermes-reboot-helper.path`
- Create: `deploy/hermes-reboot-helper.sh`
- Modify: `deploy/install-on-ubuntu.sh`
- Modify: `tests/test_deploy_assets.py`

**Interfaces:**
- Consumes only root-owned validated request file/control directory; performs fixed delayed `systemctl reboot`, never input-derived command.
- Plugin/service can create exactly one typed request through narrowly writable spool mechanism; cannot modify helper/unit.

- [ ] Write RED unit/assets/harness tests for ownership/modes, symlink rejection, exact request schema/nonce consumption, no arbitrary argv/shell, replay removal and service isolation.
- [ ] Implement helper and units with root-owned trusted ancestors, `NoNewPrivileges`, restrictive filesystem view and fixed reboot operation.
- [ ] Run deploy tests and native `systemd-analyze verify` on Ubuntu before enabling.
- [ ] Commit `ops: add confirmed host reboot helper`.

### Task 7: Documentation, security review and acceptance

**Files:**
- Modify: `README.md`
- Modify: public/release tests.

**Interfaces:**
- Produces user-facing command reference, token warning, owner recovery procedure and maintenance-window reboot acceptance.

- [ ] Add RED README contracts for every command, authorization boundary, no-OMEMO token warning, installer prompt scope and reboot confirmation/cancel/cooldown.
- [ ] Update concise Russian README without private identifiers.
- [ ] Run full suite, secret/privacy scan, diff check and whole-branch security review.
- [ ] Publish only through clean one-root-commit release repository from the public deployment plan.
- [ ] Install from GitHub; test commands across owner/trusted/denied/MUC. Test actual host reboot only in an explicitly agreed maintenance window; otherwise verify helper without executing reboot.
