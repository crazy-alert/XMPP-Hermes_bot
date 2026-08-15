# Настройка Hermes и изображения в XMPP

## Цель

Сделать owner-команды XMPP реальной, атомарной настройкой Hermes Agent и добавить генерацию/редактирование изображений с доставкой результата в XMPP.

## Факты

- Hermes разделяет текстовую модель и генератор изображений. Текст использует OpenAI-compatible Chat Completions, изображения обслуживает отдельный инструмент `image_generate` и provider.
- Текущий `admin.json` хранит `model`, `endpoint` и token, но не применяет их к `$HERMES_HOME/config.yaml` и `$HERMES_HOME/.env`.
- Встроенные платформы Hermes распознают `MEDIA:<url>`. Текущий XMPP-адаптер умеет отправлять только текст.

## Решение

1. Новый runtime-конфигуратор атомарно синхронизирует `admin.json` с `$HERMES_HOME/config.yaml` и `.env` (оба 0600, от пользователя `hermes`), не меняя посторонние настройки и не возвращая token.
2. `/endpoint set` принимает API base URL, например `https://api.example/v1`, и отвергает leaf-маршруты `/chat/completions`, `/images/generations`, `/responses`, `/messages`.
3. Текстовый provider создаётся как `xmpp-ai` с transport `openai_chat`; заданная модель хранится как `xmpp-ai/<model>`.
4. Новый image backend использует тот же token, но самостоятельно строит `<base-url>/images/generations`, поддерживает `b64_json` и URL-ответы. Image model настраивается отдельно.
5. XMPP transport загружает локальный файл через XEP-0363, отправляет XEP-0066 OOB URL и текстовый URL для совместимости. Готовый HTTPS URL пересылается без повторной загрузки.

## Команды

- `/model set <model>` — текстовая модель.
- `/endpoint set <base-url>` — общий base URL.
- `/token set <token>` — общий token.
- `/image model set <model>` — image model.
- `/image status` — image model и готовность без token.

## Ошибки и границы

- Разрешены HTTPS или loopback HTTP.
- При ненастроенном text AI trusted получает сообщение для владельца, owner — точную инструкцию.
- При ненастроенной image model инструмент получает понятную ошибку, а бот не молчит.
- Входящие изображения и OMEMO 2 не входят в этот этап.
- До VPS-применения deployment-часть проходит отдельный scoped review; XMPP-сервер и аккаунты не меняются.
