# Public XMPP Hermes Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Подготовить универсальную публичную поставку XMPP-плагина Hermes Agent, устанавливаемую одной bootstrap-командой из `crazy-alert/XMPP-Hermes_bot` без сведений о частной инфраструктуре.

**Architecture:** Корневой bootstrap `installer.sh` получает зафиксированный Git ref во временный staging, подтверждает фактический commit и передаёт управление существующему транзакционному `deploy/install-on-ubuntu.sh`. Runtime-конфигурация отделена от кода и хранится в защищённых системных файлах; README ведёт нового пользователя от требований до smoke test и отката.

**Tech Stack:** Bash, Git, Ubuntu 24.04+, systemd, Python/pytest, Hermes Agent platform plugins, Slixmpp.

## Global Constraints

- Публичный GitHub repository: `https://github.com/crazy-alert/XMPP-Hermes_bot`.
- Ни один отслеживаемый текстовый файл не содержит известных частных доменов,
  IP-адресов, JID или model endpoint из исходного deployment brief. Тест хранит
  denylist вне публикуемых fixtures и сообщает только путь совпавшего файла.
- Примеры идентификаторов используют только `example.com`/`example.net`; секреты представлены пустыми значениями или инструкциями, но не fake secret-looking строками.
- Bootstrap по умолчанию не исполняет изменяемую вершину `main`: ref/commit задаётся явно, фактический checkout commit проверяется до запуска внутреннего installer.
- `/etc/hermes/hermes.env`, Hermes config/memory и XMPP room state сохраняются при повторной установке.
- Сервис работает как `hermes`, остаётся остановленным до настройки секретов и успешной проверки.
- Shell/deployment assets имеют LF; root traversal через пользовательские каталоги fail-closed для symlink и неожиданных типов.
- Никакие credentials, server acceptance artifacts, `.superpowers/`, caches или `__pycache__` не публикуются.
- GitHub publication создаётся в отдельном allowlist staging repository с новой
  object database и ровно одним root-коммитом; development history не переносится.

---

### Task 1: Generic public configuration and privacy audit

**Files:**
- Modify: `hermes-xmpp/plugin.yaml`
- Modify: `deploy/hermes.env.example`
- Modify: `tests/test_deploy_assets.py`
- Modify: `tests/test_adapter.py`
- Modify: `tests/test_client_events.py`
- Modify: `tests/test_policy.py`
- Modify: `tests/test_state.py`
- Create: `tests/test_public_release.py`

**Interfaces:**
- Consumes: текущие env contracts `XMPP_JID`, `XMPP_PASSWORD`, `XMPP_ALLOWED_USERS`, `XMPP_NICK`, `XMPP_STATE_PATH`, TLS settings.
- Produces: нейтральные defaults/examples и repository-wide privacy gate.

- [ ] **Step 1: Write failing privacy and generic-template tests**

`tests/test_public_release.py` должен перечислять tracked text files через `git ls-files`, декодировать UTF-8 и отклонять private domain/IP/JID/endpoint. Отдельно проверить exact generic dotenv values:

```dotenv
HERMES_HOME=/var/lib/hermes/.hermes
XMPP_JID=bot@example.com/Hermes
XMPP_ALLOWED_USERS=admin@example.com
XMPP_NICK=Hermes
XMPP_STATE_PATH=/var/lib/hermes/.hermes/xmpp/rooms.json
```

Run: `.venv/Scripts/python.exe -m pytest -q tests/test_public_release.py`

Expected: FAIL на существующих private values.

- [ ] **Step 2: Replace private fixtures and examples**

Заменить инфраструктурные значения на согласованные `example.com` fixtures, не меняя route/session semantics. Plugin manifest не должен требовать конкретного домена или provider.

- [ ] **Step 3: Verify genericization**

Run: `.venv/Scripts/python.exe -m pytest -q tests/test_public_release.py tests/test_policy.py tests/test_state.py tests/test_client_events.py tests/test_adapter.py tests/test_deploy_assets.py`

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add hermes-xmpp deploy/hermes.env.example tests
git commit -m "refactor: обезличить публичную конфигурацию XMPP"
```

### Task 2: Verified GitHub bootstrap installer

**Files:**
- Create: `installer.sh`
- Modify: `.gitattributes`
- Modify: `tests/test_deploy_assets.py`
- Modify: `tests/test_public_release.py`

**Interfaces:**
- Consumes: repository URL `https://github.com/crazy-alert/XMPP-Hermes_bot.git`, exact release ref/commit, `deploy/install-on-ubuntu.sh`.
- Produces: `installer.sh [--ref <40-hex-commit-or-tag>]`, temporary staging cleanup, delegated transactional install и атомарную интерактивную конфигурацию.

- [ ] **Step 1: Write failing behavioral bootstrap tests**

Harness substitutes local fake `git`, `apt-get` and inner installer, then asserts:

- non-root and non-Ubuntu fail before network/mutation;
- default immutable ref is present and no implicit `main` checkout is executed;
- clone/fetch occurs into a fresh `mktemp -d` staging directory;
- `git rev-parse HEAD` must equal the resolved expected commit before inner installer executes;
- mismatch/tampered checkout fails without inner installer;
- staging is removed on success and failure;
- arguments are quoted and no secret values are accepted or logged.
- мастер спрашивает XMPP host, port, TLS mode, bot JID/resource/nick, скрытый
  XMPP password и непустой список доверенных bare JID;
- невалидные host/JID/port/TLS и прерванный ввод не заменяют существующий env;
- новый env записывается через temporary file, mode `0600`, atomic rename, а
  секреты не появляются в argv/stdout/stderr;
- provider/model/custom endpoint задаются опционально; при пропуске README ведёт
  пользователя через `hermes model`, а service остаётся остановленным.

Run: `.venv/Scripts/python.exe -m pytest -q tests/test_deploy_assets.py -k bootstrap`

Expected: FAIL because `installer.sh` is absent.

- [ ] **Step 2: Implement minimal bootstrap**

Use `set -Eeuo pipefail`, unconditional root/Ubuntu checks, `apt-get install --no-install-recommends ca-certificates git`, `mktemp -d`, cleanup trap, `git init` + remote/fetch exact ref (or depth-one clone where tag resolves immutably), verify a 40-hex commit via `git rev-parse`, then execute `bash "$stage/deploy/install-on-ubuntu.sh"`. После успешной транзакционной установки собрать XMPP/provider values интерактивно; секреты читать `read -rsp`, очищать trap-ом и атомарно записывать только в `/etc/hermes/hermes.env` с mode `0600`. Не запускать сервис автоматически.

- [ ] **Step 3: Enforce LF and shell syntax**

Add `installer.sh text eol=lf` to `.gitattributes`.

Run:

```bash
.venv/Scripts/python.exe -m pytest -q tests/test_deploy_assets.py -k bootstrap
bash -n installer.sh
git archive --format=tar HEAD | tar -xOf - installer.sh | python -c "import sys; d=sys.stdin.buffer.read(); assert b'\\r' not in d"
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add installer.sh .gitattributes tests/test_deploy_assets.py tests/test_public_release.py
git commit -m "feat: добавить проверяемый GitHub bootstrap installer"
```

### Task 3: Public README and operator workflow

**Files:**
- Rewrite: `README.md`
- Modify: `tests/test_public_release.py`
- Modify: `tests/test_deploy_assets.py`

**Interfaces:**
- Consumes: generic env template, root bootstrap, transactional installer, `hermes model`, systemd service.
- Produces: self-contained Russian operator guide.

- [ ] **Step 1: Write failing README contract tests**

Assert sections and commands for: purpose/capabilities, architecture, Ubuntu/Docker/Hermes prerequisites, one-command bootstrap from `crazy-alert/XMPP-Hermes_bot`, manual pinned checkout, protected interactive XMPP password entry, `hermes model`, Docker terminal backend, `hermes doctor`, systemd verify/start/status/logs, DM/MUC acceptance, update, troubleshooting, security and rollback. Assert no command pipes remote content directly to root shell without a visible verification option.

Run: `.venv/Scripts/python.exe -m pytest -q tests/test_public_release.py -k readme`

Expected: FAIL against current infrastructure-specific README.

- [ ] **Step 2: Rewrite README in concise Russian**

First paragraph: Hermes Agent XMPP bot for allowed DMs and private MUCs. Explain mention/reply routing, isolated sessions, saved rooms, TLS and Docker tool backend. Provide quick install plus a manual auditable alternative. Use placeholders `bot@example.com`, `admin@example.com`, `https://llm.example.com/v1`, never real infrastructure values.

- [ ] **Step 3: Verify documentation contracts**

Run: `.venv/Scripts/python.exe -m pytest -q tests/test_public_release.py tests/test_deploy_assets.py`

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add README.md tests/test_public_release.py tests/test_deploy_assets.py
git commit -m "docs: описать установку и работу XMPP Hermes Bot"
```

### Task 4: Release audit, native acceptance, and GitHub publication

**Files:**
- Modify only if review requires: files named by findings.
- Verify on Ubuntu: installed service/config/plugin/state.

**Interfaces:**
- Consumes: reviewed Tasks 1-3 plus completed core/deployment commits.
- Produces: public GitHub repository and sanitized acceptance report.

- [ ] **Step 1: Run full local release audit**

```bash
.venv/Scripts/python.exe -m pytest -q
git diff --check
git status --short
python -m pytest -q tests/test_public_release.py -k private_identifiers
```

Expected: tests PASS, diff clean, grep no matches, status contains no unintended files.

- [ ] **Step 2: Whole-branch security review**

Review from branch merge-base through HEAD for authorization, stanza routing, persistence races, lifecycle/reconnect, secret handling, installer transactionality, symlink traversal, bootstrap supply-chain validation and documentation accuracy. Fix all Critical/Important findings through one reviewed fix wave.

- [ ] **Step 3: Native Ubuntu bootstrap and service acceptance**

On an approved Ubuntu 24.04 VPS, use the published/pinned bootstrap or an equivalent archive of the exact commit, then run `systemd-analyze verify`, `hermes doctor`, service start/restart, connected-user check, DM/MUC scenarios and secret-free log inspection. Record only pass/fail and sanitized stanza IDs.

- [ ] **Step 4: Publish intentionally**

Скопировать только audited allowlist tracked files в новый staging без `.git`,
инициализировать новый repository, сделать один root-коммит и проверить:

```bash
git rev-list --count HEAD
git log --oneline --decorate
git fsck --full --no-reflogs
```

Expected: count `1`; object database содержит только новый tree/blob/commit. Затем
повторить privacy/secret scan и тесты внутри staging, настроить remote только там,
push в `https://github.com/crazy-alert/XMPP-Hermes_bot.git` и проверить remote SHA/
default-branch files. Не переносить development `.git`; не force-push и не
перезаписывать unrelated remote history без явного подтверждения пользователя.

- [ ] **Step 5: Record rollback**

Document `sudo systemctl disable --now hermes-gateway`; preserve ejabberd accounts/rooms and all persistent Hermes data unless a separate explicit deletion is requested.
