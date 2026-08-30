# Telemax

Личный мост между MAX и Telegram. Каждый выбранный диалог MAX становится
отдельным приватным Telegram-ботом: со своим именем, аватаром, уведомлениями и
поиском. Guardian — один управляющий бот — подключает MAX, создаёт мосты и
показывает проблемы доставки.

> Неофициальный open-source проект, не связанный с MAX или Telegram. MAX
> подключается через сторонний user-account API, поэтому перед обновлением стоит
> читать changelog и сохранять каталог данных.

```text
MAX: Анна   ⇄   Telegram-бот «Анна · MAX»
MAX: Работа ⇄   Telegram-бот «Работа · MAX»
                         │
                      Guardian
```

## Как выглядит первый запуск

Терминал нужен один раз — чтобы безопасно создать Telegram user-session и
первого Guardian. Дальше всё происходит в Telegram:

```text
install.sh
  → api_id + api_hash
  → QR Telegram
  → Guardian создаётся через @BotFather
  → user service запускается
  → Telegram: часовой пояс → вход в MAX → выбор диалогов
```

Owner ID вводить не нужно: Telemax читает его из отсканированной сессии. Номер,
SMS-код и 2FA от MAX вводятся уже в приватном чате Guardian и не требуют держать
SSH открытым.

## Что умеет

- текст, форматирование, ответы, правки и удаления в обе стороны;
- фото, видео, файлы, голосовые, видеозаметки, стикеры и альбомы;
- реакции, typing, read receipts и статус контакта;
- импорт истории и опциональный перенос собственных сообщений из MAX;
- отдельный Telegram-бот на контакт и один Guardian для управления;
- durable inbox/outbox, bounded retry, TTL, `failed` и честный `ambiguous`;
- восстановление после рестарта без тихой потери уже принятой работы;
- host-first установка без root и изолированный Docker как опция.

Exactly-once не обещается там, где удалённая сторона приняла отправку, но ответ
потерялся: автоматический повтор мог бы создать дубль. Такая доставка остаётся
видимой и требует решения владельца в Guardian.

## Быстрая установка — рекомендуется

Нужны Linux, `git`, `curl`, Telegram и MAX. Пользовательский `systemd` должен
быть доступен; root не требуется. Python 3.12 и зависимости установщик
переиспользует или подтянет через закреплённый `uv`.

Перед стартом получите `api_id` и `api_hash` в
[my.telegram.org → API development tools](https://my.telegram.org/). Это не
данные бота и не замена QR: они идентифицируют Telegram-приложение, которым
Telethon открывает MTProto-соединение; QR уже авторизует в нём ваш аккаунт.
Поэтому оба значения нужны и при первом входе, и при повторном открытии
сохранённой session после рестарта. Telegram описывает эту пару как
[параметры, обязательные для user authorization](https://core.telegram.org/api/obtaining_api_id).

```bash
git clone https://github.com/aipolukhin/telemax.git ~/telemax
cd ~/telemax
./install.sh
```

Когда появится QR:

1. Откройте Telegram → **Настройки → Устройства**.
2. Нажмите **Подключить устройство** и отсканируйте код.
3. Откройте созданный Guardian — дальнейшие экраны проведут через MAX.

Установщик создаёт `.venv`, локальные `config.yaml`/`.env`, приватные session-
файлы и user unit `telemax.service`. Повторный запуск сохраняет существующие
сессии и настройки.

```bash
systemctl --user status telemax.service
journalctl --user -u telemax.service -f
```

Для второй установки на том же сервере:

```bash
./install.sh --config config-family.yaml --instance family
```

## Docker — опционально

Docker не является дефолтом. Он полезен, если на хосте уже принят контейнерный
deployment или нет подходящего user-systemd:

```bash
git clone https://github.com/aipolukhin/telemax.git ~/telemax
cd ~/telemax
./docker.sh setup
```

Setup также покажет Telegram QR, создаст Guardian, затем запустит Compose.
Конфиг, Telegram/MAX sessions, SQLite и media остаются на хосте в
`~/.local/share/telemax/docker`; пересборка image и `down` их не удаляют.

```bash
./docker.sh status
./docker.sh logs
./docker.sh doctor
./docker.sh restart
./docker.sh down
```

Контейнер работает без root-capabilities, с read-only root filesystem, без
публичных портов и без Docker socket. Healthcheck проверяет свежесть heartbeat
внутреннего supervisor, а не доступность внешних MAX/Telegram: временный сетевой
сбой должен быть виден в Guardian, но не превращать живой процесс в crash loop.

Другой каталог данных задаётся явно и затем используется в каждой команде:

```bash
./docker.sh setup --state-dir /absolute/path/telemax-state
TELEMAX_DOCKER_STATE=/absolute/path/telemax-state ./docker.sh status
```

## Почему нужна полная Telegram-сессия

Bot API не умеет создавать ботов, а один бот не может пройти диалог с
`@BotFather`. Поэтому Telemax подключается как дополнительное устройство
Telegram и хранит одну полную Telethon session:

- во время setup она создаёт или находит Guardian;
- в runtime через неё создаются contact-боты и принимаются owner-side
  сообщения, правки, удаления и реакции;
- приложение обрабатывает только чаты с собственными contact-ботами, но это
  ограничение кода, а не Telegram credential.

Session лежит в `data/secrets/telegram-user.session` с режимом `0600`. Сервер
нужно считать доверенным устройством Telegram. Для перевыпуска session:

```bash
.venv/bin/telemax telegram-sync
systemctl --user restart telemax.service
```

Ручное принятие уже существующего Guardian оставлено только как recovery:

```bash
./install.sh --manual-guardian
```

## Конфигурация и данные

Полный безопасный пример — [`config.example.yaml`](config.example.yaml), список
переменных — [`.env.example`](.env.example).

- `config.yaml` — owner ID, настройки и пути;
- `.env` — bot tokens, номера и Telegram API credentials, режим `0600`;
- `data/` — SQLite, очереди, MAX/Telegram sessions и временные media;
- `BRIDGE_CONFIG` или `--config` меняет путь к конфигу;
- `BRIDGE_DATA_DIR` и `BRIDGE_LOG_LEVEL` переопределяют data dir и логирование.

Ни один из этих файлов нельзя прикладывать к issue. Проверка локального
конфига:

```bash
.venv/bin/telemax validate-config
.venv/bin/telemax healthcheck
```

## Ограничения

- MAX не предоставляет стабильный публичный контракт для всех используемых
  возможностей; совместимость закреплена на `maxapi-python==2.3.1`;
- Telegram Bot API ограничивает загрузку некоторых входящих файлов; Telemax
  показывает явный placeholder вместо тихого пропуска;
- native media деградирует в обычный файл/видео при подтверждённой
  несовместимости;
- удаление бота или session вне Telemax может потребовать повторного setup;
- один Telegram owner сейчас управляет одним Guardian и одной MAX identity.

## Основа и provenance

Telemax не реализует клиенты мессенджеров с нуля:

- [PyMax](https://github.com/MaxApiTeam/PyMax) — MAX session, events и API;
- [aiogram](https://github.com/aiogram/aiogram) — Telegram Bot API;
- [Telethon](https://codeberg.org/Lonami/Telethon) — Telegram owner session;
- [aiohttp](https://github.com/aio-libs/aiohttp),
  [PyAV](https://github.com/PyAV-Org/PyAV) и
  [Pillow](https://github.com/python-pillow/Pillow) — HTTP и media pipeline.

Полный dependency/license перечень — [`THIRD_PARTY.md`](THIRD_PARTY.md).
Каталог изученных MAX↔Telegram мостов, точные SHA, лицензии и решения
«взяли / отложили / не копируем» — [`docs/provenance.md`](docs/provenance.md).

## Разработка

```bash
uv sync --extra dev
.venv/bin/python -m ruff check bridge tests
.venv/bin/python -m mypy
.venv/bin/python -m pytest -q
```

Архитектура доставки описана в
[`docs/architecture/delivery-semantics.md`](docs/architecture/delivery-semantics.md),
схема БД — в
[`docs/architecture/database-schema.md`](docs/architecture/database-schema.md),
операционные действия — в [`docs/runbook.md`](docs/runbook.md), решения — в
[`docs/adr/`](docs/adr/).

## Безопасность и лицензия

Утечки сообщайте по [`SECURITY.md`](SECURITY.md), не публикуя tokens, номера,
сообщения или session-файлы. Правила участия — [`CONTRIBUTING.md`](CONTRIBUTING.md).

Код распространяется по лицензии [Apache-2.0](LICENSE).
