# Assisted Hermes Installer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Сделать установку Docker и запуск Hermes подтверждаемыми завершающими этапами установщика и подробно описать его поведение.

**Architecture:** Существующий transactional installer сохраняется. Ранняя функция preflight проверяет Docker и при необходимости с отдельным подтверждением устанавливает Ubuntu `docker.io`; после staging и runtime-проверок отдельный финализатор предлагает `enable --now` и проверяет фактическое состояние службы.

**Tech Stack:** Bash, apt, systemd, Docker Engine, pytest harness, Ubuntu 24.04.

## Global Constraints

- Не менять ejabberd, XMPP accounts/rooms или shared packages кроме явно подтверждённой установки `docker.io`.
- Не выводить секреты или содержимое закрытого env-файла.
- Отказ, EOF и неинтерактивный stdin не считаются согласием.
- Docker, установленный до Hermes, не удаляется при rollback Hermes.
- Production installer не содержит test hooks; тестирование использует generated copy и stubs.

---

### Task 1: Docker preflight с подтверждением

**Files:**
- Modify: `tests/test_deploy_assets.py`
- Modify: `deploy/install-on-ubuntu.sh`

**Interfaces:**
- Consumes: root, Ubuntu 24.04+, stdin/TTY, `apt-get`, `systemctl`, `docker info`, `getent group docker`.
- Produces: проверенный Docker CLI, daemon и группа до создания Hermes identity.

- [ ] Добавить harness-сценарии: Docker готов; отсутствует и пользователь отказал; отсутствует и подтвердил; EOF/non-TTY; пакет или daemon не прошёл повторную проверку.
- [ ] Запустить focused tests и подтвердить ожидаемый RED на текущей немедленной ошибке `group docker отсутствует`.
- [ ] Реализовать строгий confirmation parser и установку только `docker.io` через APT, затем `systemctl enable --now docker` и повторный preflight.
- [ ] Запустить focused и весь `tests/test_deploy_assets.py`; ожидать PASS.
- [ ] Commit: `feat: offer Docker installation during setup`.

### Task 2: Проверенный запуск Hermes

**Files:**
- Modify: `tests/test_deploy_assets.py`
- Modify: `deploy/install-on-ubuntu.sh`

**Interfaces:**
- Consumes: установленный unit/runtime/env и `hermes doctor`.
- Produces: по согласию active+enabled `hermes-gateway`; по отказу stopped+disabled deployment.

- [ ] Добавить RED для согласия/отказа/EOF, ошибки doctor, отсутствующего unit, ошибки старта и ложного success без `is-active`/`is-enabled`.
- [ ] Запустить focused tests и подтвердить RED, поскольку текущий installer всегда оставляет службу disabled/stopped.
- [ ] Перед вопросом запустить doctor как `hermes`, проверить unit через `systemctl cat`; при согласии выполнить `enable --now`, затем `is-enabled` и `is-active`; при ошибке вывести только безопасные команды диагностики.
- [ ] Запустить focused и deployment suite; ожидать PASS.
- [ ] Commit: `feat: verify and optionally start Hermes service`.

### Task 3: README и acceptance

**Files:**
- Modify: `README.md`
- Modify: `tests/test_public_release.py`
- Modify: `.superpowers/HANDOFF.md`
- Modify: `.superpowers/sdd/2026-08-13-assisted-installer/progress.md`

**Interfaces:**
- Produces: точное пользовательское описание installer и прозрачный необязательный раздел AI Tunnel.

- [ ] Добавить README contract RED: Docker prompt/package, шаги installer, создаваемые пути, проверки, запуск/отказ, rollback boundary, AI Tunnel и точная ссылка `https://aitunnel.ru?r=43877`.
- [ ] Переписать раздел установки литературным русским текстом; в конце добавить отдельный раздел поддержки с явным раскрытием реферального характера ссылки.
- [ ] Запустить README tests, deployment suite, `bash -n`, Docker-based Ubuntu проверки и privacy/secret scan.
- [ ] Провести scoped deployment/security review; исправить все Critical/Important замечания.
- [ ] Обновить handoff/ledger точными commit hash и результатами проверок.
- [ ] Commit: `docs: describe assisted installation and project support`.
