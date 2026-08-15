# Hermes Agent + XMPP

Плагин подключает Hermes Agent к XMPP-серверу по TLS. Поддерживается Ubuntu
24.04 и новее; Hermes работает от отдельного непривилегированного пользователя
`hermes`.

## Установка

Скопируйте репозиторий целиком на сервер и запустите установщик из его корня:

```bash
sudo bash deploy/install-on-ubuntu.sh
```

Для установки без предварительного `git clone` скачайте bootstrap-скрипт:

```bash
curl -fsSL "https://raw.githubusercontent.com/crazy-alert/XMPP-Hermes_bot/main/installer.sh?timestamp=$(date +%s%N)" \
  | sudo bash
```

Установщик по умолчанию загружает текущую ветку `main`, создаёт временный
checkout, проверяет его и удаляет временные файлы. При необходимости ветку или
тег можно выбрать через `HERMES_INSTALL_REF` или параметр `--ref`.

Docker Engine нужен Hermes как backend для запуска инструментов в изолированных
контейнерах. Установщик сначала проверяет Docker CLI, группу `docker` и доступность
daemon. Если проверка не проходит, он предложит установить пакет Ubuntu
`docker.io`. Установка выполняется только после явного ответа `y`, `yes`, `д` или
`да`; отказ, EOF и неинтерактивный запуск безопасно завершают работу без
дальнейших изменений.

После согласия установщик обновляет индексы пакетов, устанавливает Docker,
включает его службу и повторяет проверку. Уже установленный Docker не заменяется
и не удаляется.

Затем установщик:

1. проверяет Ubuntu, существующие пути, владельцев и типы файлов;
2. создаёт системного пользователя и группу `hermes`, закрытые каталоги и файл
   `/etc/hermes/hermes.env` с правами `0600`;
3. устанавливает Hermes и зависимости, копирует только allowlist-файлы плагина и
   проверяет systemd units через `systemd-analyze verify`;
4. устанавливает узкий root-owned helper `hermes-reboot-helper`, который принимает
   только одноразовый типизированный запрос на перезагрузку;
5. запускает `hermes doctor` без вывода секретов;
6. в самом конце спрашивает, включить автозапуск и запустить `hermes-gateway`.

При отказе от последнего шага служба остаётся остановленной и отключённой. При
ошибке до фиксации конфигурации установщик откатывает свои временные изменения;
Docker, пакеты и уже существующие данные Hermes автоматически не удаляются.

## Какие данные вводятся

В интерактивном режиме установщик запрашивает ровно семь значений XMPP:

- хост и порт;
- режим TLS (сейчас поддерживается `direct`);
- полный JID бота вместе с resource;
- отображаемое имя бота;
- пароль XMPP (вводится скрыто);
- первый owner bare JID (`First owner bare JID`).

Установщик задаёт эти вопросы по-русски: сервер и порт XMPP, полный JID бота с
ресурсом, пароль и первый bare JID владельца. Ник автоматически берётся из
ресурса JID. Используется единственный поддерживаемый режим TLS — `direct`,
поэтому отдельно выбирать его не нужно.

XMPP-аккаунт установщик не создаёт: зарегистрируйте его в своём ejabberd
заранее. Модель, endpoint, API-токен и дополнительные trusted JID настраиваются
позже owner-only командами в личном чате. Токен не следует отправлять через
нешифрованный канал: XMPP API-token DM работает без OMEMO/E2E и не использует
сквозное шифрование, поэтому архивы
сервера и история клиента могут его сохранить.

## Ручная установка без установщика

Этот вариант предназначен для администратора, которому нужно контролировать
каждую команду. Он не выполняет автоматический rollback, поэтому перед началом
сделайте резервные копии и выполняйте команды от `root` на Ubuntu 24.04+.

### 1. Зависимости и Docker

```bash
apt-get update
apt-get install -y ca-certificates curl git build-essential pkg-config libssl-dev libffi-dev python3-minimal docker.io
systemctl enable --now docker
getent group docker
docker info
```

Создайте системные учётные записи и добавьте `hermes` в группу Docker:

```bash
groupadd --system hermes 2>/dev/null || true
id hermes >/dev/null 2>&1 || useradd --system --create-home --home-dir /var/lib/hermes --gid hermes --shell /usr/sbin/nologin hermes
usermod -aG docker hermes
install -d -o root -g root -m 0755 /var/lib/hermes
install -d -o hermes -g hermes -m 0700 /var/lib/hermes/.hermes /var/lib/hermes/.local /var/lib/hermes/.cache
```

### 2. Hermes Agent и плагин

Установите ту же pinned-версию Hermes Agent, что указана в начале
`deploy/install-on-ubuntu.sh`, используя официальный скрипт Hermes под
пользователем `hermes` (без root-доступа). Затем установите зависимости XMPP:

```bash
sudo -u hermes -H env HOME=/var/lib/hermes HERMES_HOME=/var/lib/hermes/.hermes \
  bash /path/to/hermes-agent/scripts/install.sh --skip-setup --skip-browser \
  --dir /var/lib/hermes/.hermes/hermes-agent --hermes-home /var/lib/hermes/.hermes
sudo -u hermes -H /var/lib/hermes/.hermes/bin/uv pip install \
  --python /var/lib/hermes/.hermes/hermes-agent/venv/bin/python 'slixmpp>=1.12,<2' 'slixmpp-omemo==2.2.0' pytest
```

Скопируйте `hermes-xmpp/adapter.py`, `plugin.yaml` и каталог `xmpp_bridge/` в
`/var/lib/hermes/.hermes/plugins/xmpp-platform`, назначьте владельца `hermes:hermes`
и права `0700` на каталог и файлы. Не копируйте `.git`, тестовые артефакты или
символические ссылки.

### 3. Конфигурация XMPP

Создайте `/etc/hermes/hermes.env` с правами `0600` и владельцем `root:root`.
Заполните значения, полученные от вашего XMPP-сервера:

```dotenv
HERMES_HOME=/var/lib/hermes/.hermes
XMPP_JID=bot@example.org/Hermes
XMPP_ALLOWED_USERS=owner@example.org
XMPP_NICK=Hermes
XMPP_STATE_PATH=/var/lib/hermes/.hermes/xmpp/rooms.json
XMPP_HOST=example.org
XMPP_PORT=5223
XMPP_TLS_MODE=direct
XMPP_ADMIN_STATE_PATH=/var/lib/hermes/.hermes/xmpp/admin.json
# Добавьте XMPP_PASSWORD вручную; само значение намеренно не приводится в документации.
```

Сразу замените демонстрационные значения; не помещайте реальные секреты в Git,
командную историю или README. Файл `admin.json` создайте от `hermes`, оставив
единственным owner только ваш bare JID, например `owner@example.org`.

### 4. systemd и проверка

Скопируйте `deploy/hermes-gateway.service` в
`/etc/systemd/system/hermes-gateway.service`, заменив путь `ExecStart` на
`/var/lib/hermes/.hermes/hermes-agent/venv/bin/hermes gateway run`. Скопируйте
также `hermes-reboot-helper.sh`, `.service` и `.path` в пути, указанные в самих
unit-файлах; helper должен принадлежать `root:root`, иметь режим `0755`, а его
spool и control-каталоги — режимы `0730` и `0700` соответственно.

```bash
systemd-analyze verify /etc/systemd/system/hermes-gateway.service
systemctl daemon-reload
sudo -u hermes -H env HERMES_HOME=/var/lib/hermes/.hermes \
  /var/lib/hermes/.hermes/hermes-agent/venv/bin/hermes doctor
systemctl enable --now hermes-reboot-helper.path
systemctl enable --now hermes-gateway
systemctl is-enabled hermes-gateway
systemctl is-active hermes-gateway
```

Ручной способ не создаёт XMPP-аккаунт и не изменяет ejabberd. Если вы пропустили
проверку владельцев, режимов или версии Hermes, дальнейшее обслуживание и
безопасный rollback становятся вашей ответственностью.

## После установки

Проверить состояние службы и диагностику можно так:

```bash
sudo systemctl status hermes-gateway --no-pager
sudo -u hermes -H env HERMES_HOME=/var/lib/hermes/.hermes hermes doctor
```

Основные owner-only команды доступны в DM боту: настройка модели и провайдера,
смена токена, управление trusted JID, `ping` (ответ `pong`), а также безопасная
перезагрузка через двухшаговое подтверждение. Команды управления не меняют
конфигурацию ejabberd и не требуют настройки комнат.

Для остановки службы:

```bash
sudo systemctl disable --now hermes-gateway
```

Перед удалением сделайте резервные копии `/var/lib/hermes` и
`/etc/hermes/hermes.env`. Не удаляйте Docker автоматически: он может быть нужен
другим приложениям на сервере.

## Обновления и безопасность

Установщик запускается только из проверенного GitHub commit. Обновления Hermes
плагина выполняйте из опубликованного релиза, предварительно проверив изменения
и сохранив резервную копию. Не передавайте установщику секреты через аргументы
командной строки или журналы shell.

## Поддержка проекта

Проект тестировался с сервисом AI Tunnel. Если вы зарегистрируетесь в нём по этой
реферальной ссылке [https://aitunnel.ru?r=43877](https://aitunnel.ru?r=43877),
это поддержит дальнейшую разработку проекта.

В проекте можно использовать любые OpenAI-compatible провайдеры.
