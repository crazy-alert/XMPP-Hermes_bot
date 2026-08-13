# Hermes Agent + XMPP

Плагин подключает Hermes Agent к ejabberd через TLS как
`hermes@aversa.run/Hermes`. Развёртывание рассчитано на Ubuntu 24.04 или новее
и отдельного непривилегированного пользователя `hermes`.

## Установка

На сервере должен уже работать Docker Engine с группой `docker`, а этот
репозиторий должен быть скопирован целиком. Из корня репозитория выполните:

```bash
sudo bash deploy/install-on-ubuntu.sh
```

Установщик не запускает и не включает службу. Он сохраняет существующий
`/etc/hermes/hermes.env`; при первой установке создаёт его с правами `0600`.

## XMPP-аккаунт и секреты

Создайте пароль без записи в историю shell и зарегистрируйте аккаунт в
контейнере ejabberd:

```bash
read -rsp 'Пароль XMPP для Hermes: ' BOT_PASSWORD; printf '\n'
docker compose exec ejabberd ejabberdctl register hermes aversa.run "$BOT_PASSWORD"
unset BOT_PASSWORD
```

Настройте провайдера интерактивно. Для custom OpenAI-compatible endpoint
укажите `https://api.aitunnel.ru/v1`, модель `gpt-5.6-sol`, а API-ключ вводите
только в секретный prompt команды:

```bash
sudo -u hermes -H env HERMES_HOME=/var/lib/hermes/.hermes hermes model
```

Затем откройте закрытый env-файл через `sudoedit` и добавьте `XMPP_PASSWORD`,
а также переменную provider key, которую запросила команда `hermes model`. Не
копируйте секреты в README или командную строку:

```bash
sudoedit /etc/hermes/hermes.env
```

## Проверка и запуск

Настройте Docker backend и проверьте Hermes до включения автозапуска:

```bash
sudo -u hermes -H env HERMES_HOME=/var/lib/hermes/.hermes hermes config set terminal.backend docker
sudo -u hermes -H env HERMES_HOME=/var/lib/hermes/.hermes hermes doctor
sudo systemctl enable --now hermes-gateway
sudo systemctl status hermes-gateway --no-pager
sudo journalctl -u hermes-gateway -n 100 --no-pager
```

## Остановка и откат

```bash
sudo systemctl disable --now hermes-gateway
```

Эта команда не меняет ejabberd и другие XMPP-клиенты. Перед удалением данных
сделайте резервную копию `/var/lib/hermes` и `/etc/hermes/hermes.env`.
