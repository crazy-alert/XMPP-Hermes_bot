# GitHub Release Updates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Уведомлять owners о проверенных GitHub Releases и выполнять подтверждённое транзакционное обновление с rollback.

**Architecture:** Непривилегированный checker парсит строго ограниченные release metadata и выдаёт typed availability events. Owner confirmation создаёт typed spool request; отдельный root-owned one-shot helper повторно проверяет manifest/assets и активирует release с health rollback.

**Tech Stack:** Python stdlib HTTP/JSON, asyncio, systemd, Bash, pytest.

## Global Constraints

- Repository fixed to `crazy-alert/XMPP-Hermes_bot`; mutable `main` never installed.
- Stable channel accepts published non-draft non-prerelease Releases only.
- Six-hour interval with jitter, bounded timeout/body/backoff, ETag/304 and deduplicated notifications.
- No automatic install; same-owner DM confirmation required within 60 seconds.
- Manifest version/commit/assets/size/SHA-256 required and reverified by root helper.
- Config/secrets/owners/trusted/memory/room state never overwritten.
- No arbitrary URL/path/command, shell from notes, or general sudo/root for bot.

---

### Task 1: Release metadata checker

**Files:** Create `hermes-xmpp/xmpp_bridge/updates.py`, `tests/test_updates.py`.

- [ ] RED tests: ETag/304, stable filtering, prerelease opt-in, repository/API URL fixed, response timeout/size/schema, manifest/assets/version/40hex commit/SHA validation, downgrade rejection, notes truncation/control cleanup, dedup state, bounded backoff/jitter, secret-free repr/errors.
- [ ] Implement pure parser/checker with injected HTTP/clock/random; no install or filesystem mutation beyond an abstract state callback.
- [ ] Run focused and commit `feat: check verified GitHub releases`.

### Task 2: Update confirmation commands

**Files:** Modify `commands.py`, create/modify update confirmation tests.

- [ ] RED `/update check|status|install|cancel`, same-owner confirm, timeout/replay/version availability change; typed immutable request only.
- [ ] Reuse generic confirmation semantics without exposing manifest digest/code in logs.
- [ ] Run command/update suites and commit.

### Task 3: Root-owned transactional updater

**Files:** Create updater script/unit/path and behavioral tests; modify installer allowlist.

- [ ] RED manifest mismatch, oversize, archive traversal/symlink, wrong repo/ref, test/bash/systemd failure, health timeout, successful swap, rollback and persistent-data preservation.
- [ ] Implement fixed-repository downloader, root-owned staging, exact checks, reviewed installer, activation/health and rollback. No arbitrary inputs.
- [ ] Native systemd verify on Ubuntu, commit and security review.

### Task 4: Periodic integration and docs

**Files:** Modify adapter/supervisor/README/tests.

- [ ] Wire six-hour jittered checker, DM owners once, persist notification version, command-triggered check and result notifications.
- [ ] Document channels, confirmation, rollback and troubleshooting.
- [ ] Full suite/security review/native update acceptance before public release.
