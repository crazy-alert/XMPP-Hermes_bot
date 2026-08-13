# Служебные команды XMPP Hermes Bot

## Цель и границы

Бот должен запускаться и отвечать на служебные команды до настройки AI provider.
Bootstrap installer спрашивает только параметры XMPP-подключения и первый owner;
model, endpoint, API token, trusted JID и дальнейшие owners настраиваются через DM
с ботом.

Без OMEMO API token не обладает end-to-end защитой и может сохраниться в архиве
XMPP-сервера или клиентской истории. README явно предупреждает об этом. Сам plugin
никогда не возвращает token, не пишет его в application logs и отображает только
маску.

## Авторизация

- Служебные изменяющие команды принимаются только в личном чате от owner bare JID.
- Команды из MUC и от trusted, но не owner, не выполняются и не раскрывают
  конфигурацию.
- Первый owner задаётся установщиком.
- Последнего owner удалить нельзя; self-removal разрешён только если останется
  другой owner.
- Trusted users получают обычный доступ к Hermes, но не административные права.

## Команды

- `ping` в любом регистре с окружающими пробелами отвечает `pong` всем
  авторизованным DM-пользователям и не вызывает модель.
- `/help` показывает команды, доступные вызывающему.
- `/status` и `/config` показывают состояние XMPP/provider/model без секретов.
- `/model set <model>` меняет model identifier.
- `/endpoint set <https-url>` меняет OpenAI-compatible endpoint; разрешён только
  HTTPS, кроме явно локальных loopback endpoints.
- `/token set <token>` сохраняет provider token и отвечает только фактом успеха и
  короткой маской.
- `/trust list|add|remove <bare-jid>` управляет trusted JID.
- `/owner list|add|remove <bare-jid>` управляет owners с invariant последнего owner.
- `/doctor` запускает ограниченную безопасную проверку конфигурации.
- `/restart` просит управляющий процесс применить новую конфигурацию; команда не
  запускает произвольный shell и не перезапускает ejabberd.
- `/server reboot` создаёт запрос на перезагрузку хоста и возвращает одноразовую
  команду `/confirm reboot <code>`. Подтверждение принимает только DM того же
  owner в течение 60 секунд. `/server reboot cancel` отменяет ожидающий запрос.
  Код одноразовый, одновременно разрешён один запрос, а после исполнения действует
  cooldown от циклических перезагрузок.

Неизвестная `/команда` получает краткую ошибку и ссылку на `/help`. Обычный текст
trusted пользователя продолжает идти в Hermes.

Отдельной настройки комнат нет. Бот отвечает trusted bare JID в любой MUC, где он
присутствует, только при mention либо reply на сообщение бота. Приглашения
принимаются только от owner/trusted JID. Для авторизации stanza должна содержать
подтверждённый real JID участника; в anonymous MUC, где доступен лишь room nick,
бот fail-closed не отвечает. Сохранённый список комнат используется только для
rejoin после рестарта, а не как allowlist комнат.

## Хранилище и применение

Owners, trusted JID, provider/model/endpoint и token хранятся в отдельном
защищённом config store под Hermes home. Запись выполняется под service user через
temporary file, `fsync`, atomic replace и mode `0600`; конкурирующие команды
сериализуются file lock. Parser валидирует полную схему и fail-closed отклоняет
битый файл. Token никогда не попадает в repr/exception/report.

Command router выполняется до создания Hermes `MessageEvent`, поэтому `/ping` и
административные команды работают при сломанном или отсутствующем provider.
Изменение runtime-настроек создаёт новый immutable snapshot; adapter применяет его
между сообщениями. Для значений, которые Hermes не может безопасно reload,
`/restart` передаёт типизированный control event supervisor, а не выполняет
`systemctl` напрямую.

Перезагрузка хоста проходит через отдельный root-owned systemd control helper с
единственной операцией reboot. Hermes service не получает общий sudo или
произвольный root shell. Helper принимает только типизированный request через
защищённый control path; после валидного подтверждения бот сначала отправляет
ответ, затем инициирует reboot с короткой задержкой. Компрометация owner account
или процесса Hermes всё равно даёт возможность вызвать отказ в обслуживании через
reboot — этот остаточный риск явно документируется.

## Проверка

Тесты покрывают регистр `ping`, bypass модели, owner/trusted/MUC matrix, последнего
owner, JID/URL validation, atomic recovery, concurrent mutations, corrupted state,
token redaction в ответах/logs/errors и reload/restart contract. Acceptance
проверяет команды из двух owners, trusted и denied JID, после чего перезапускает
service и подтверждает сохранение конфигурации. Для host reboot тестируются
same-owner DM confirmation, timeout, replay, cancel, concurrent request и cooldown;
нативный acceptance выполняется только в согласованное окно обслуживания.
