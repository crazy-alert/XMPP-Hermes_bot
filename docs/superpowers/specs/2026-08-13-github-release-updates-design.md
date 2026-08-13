# Обновления XMPP Hermes Bot из GitHub Releases

## Назначение

Бот периодически проверяет новые GitHub Releases проекта
`crazy-alert/XMPP-Hermes_bot`, уведомляет owners в DM и устанавливает выбранный
release только после двухшагового подтверждения. Обновление из изменяемой ветки
`main` не поддерживается.

## Проверка версии

Непривилегированный checker раз в шесть часов с jitter запрашивает GitHub Releases
API с коротким timeout, условными ETag-запросами и ограничением размера ответа.
Принимаются только published, non-draft и non-prerelease releases, если owner явно
не включил prerelease channel. Текущая и уже уведомлённая версии хранятся атомарно,
чтобы после рестарта не повторять уведомления.

Release должен содержать manifest с version, source commit, именами assets,
размерами и SHA-256. Checker рассматривает текст release notes только как
неисполняемые данные, нормализует длину и не передаёт из него команды shell.

## Команды

- `/update check` выполняет немедленную проверку.
- `/update status` показывает текущую, доступную и последнюю уведомлённую версии.
- `/update install <version>` создаёт pending action и показывает точную версию.
- `/confirm update <code>` принимает только тот же owner в DM в течение 60 секунд.
- `/update cancel` отменяет pending action.

Установка не начинается автоматически. Один pending update одновременно; replay,
timeout и версия, переставшая быть доступной, fail-closed отменяются.

## Привилегированный updater

Root-owned one-shot systemd unit получает только allowlisted release version и
manifest digest через защищённый spool. Он повторно запрашивает release metadata,
скачивает assets во временный root-owned staging, проверяет repository identity,
manifest, size и SHA-256, запрещает symlink/path traversal в archive и запускает
reviewed installer из точного release.

Перед заменой сохраняются plugin/unit/runtime metadata. Env, admin config, token,
owners/trusted JID, Hermes memory и room rejoin state никогда не входят в backup
swap и не перезаписываются. До activation выполняются tests, `bash -n` и
`systemd-analyze verify`. После restart helper ждёт service active, XMPP readiness
marker и bounded health check. При сбое автоматически восстанавливает предыдущие
assets и перезапускает старую версию.

Hermes process не получает sudo/root shell и не управляет URL/path/command. Helper
никогда не устанавливает `main`, произвольный repository или неподписанный local
checkout.

## Уведомления и сбои

Owners получают одно уведомление о доступном release и итог update/rollback.
Сетевые ошибки применяют bounded exponential backoff без spam. API token GitHub не
требуется для публичного repository; при rate limit сохраняется следующий срок
проверки. Логи содержат version/digest/result, но не XMPP/provider credentials.

## Проверка

Unit tests покрывают ETag/304, draft/prerelease/channel, downgrade rejection,
notification deduplication, pending confirmation и redaction. Behavioral updater
harness проверяет manifest mismatch, oversized asset, traversal/symlink archive,
tests/systemd failure, health timeout, successful activation и полный rollback.
Native acceptance обновляет только тестовую/предыдущую версию до опубликованного
release; production update выполняется после отдельного подтверждения owner.
