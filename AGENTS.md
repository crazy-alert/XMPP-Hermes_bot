# Инструкции для агентов

## Язык и стиль

- Общайтесь с пользователем и пишите документацию по-русски, если он не попросил
  иначе.
- Объясняйте результат и риски кратко и предметно. Не публикуйте секреты,
  приватные JID, домены, IP-адреса или server-specific журналы.

## Восстановление после прерывания

Перед продолжением незавершённой работы:

1. прочитайте `.superpowers/HANDOFF.md`, если файл существует;
2. определите активный план по `.superpowers/sdd/*/progress.md`;
3. прочитайте только соответствующие task brief/report и публичный plan/spec;
4. сверьте `git status --short`, последние commits и активных subagents;
5. не повторяйте завершённые tasks или уже выполненные операции на сервере.

После каждого существенного commit, review verdict, VPS mutation/rollback или
изменения требований обновляйте `.superpowers/HANDOFF.md` и соответствующий
ledger. Записывайте точные commit hash и результаты проверок, но никогда секреты.

`.superpowers/` — локальный ignored workspace. Его содержимое, caches, reports,
private denylist и acceptance artifacts не включаются в публичный release.

## Разработка

- Следуйте TDD: сначала наблюдаемый RED, затем минимальный GREEN и полный
  релевантный прогон.
- Deployment/security изменения проходят отдельный scoped review до применения
  на реальном сервере.
- Не запускайте installer на VPS, пока текущий deployment commit не прошёл review.
- Не изменяйте ejabberd, Docker, XMPP accounts/rooms или shared packages, если это
  явно не входит в текущий task.
- Сохраняйте чужие и параллельные изменения; коммитьте только намеренные файлы.

## Публичный release

- Development `.git` history не публикуется.
- Соберите release в отдельном allowlist staging без `.git`, `.superpowers`,
  caches, credentials и server artifacts.
- В staging повторите тесты и privacy/secret scan, затем создайте новый repository
  ровно с одним root-коммитом.
- Перед push проверьте `git rev-list --count HEAD`, `git fsck --full --no-reflogs`
  и состав tracked tree.
- Установка на production выполняется только из опубликованного и проверенного
  GitHub release/ref, не из development checkout.
