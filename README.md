# Telemax

Неофициальный личный мост между MAX и Telegram. Каждый выбранный диалог MAX
становится отдельным приватным Telegram-ботом, а бот-страж отвечает за настройку,
состояние и восстановление доставки.

> Проект не связан с MAX или Telegram и использует сторонние API. Перед
> обновлением проверяйте совместимость на своей установке.

```text
MAX: Контакт A  ⇄  Telegram-бот «Контакт A · MAX»
MAX: Контакт B  ⇄  Telegram-бот «Контакт B · MAX»
                         │
                    бот-страж
```

## Зачем отдельный бот на контакт

Так каждый человек остаётся обычным диалогом Telegram: со своим именем,
аватаром, уведомлениями и поиском. Страж не читает переписку — он подключает
MAX, создаёт мосты, показывает проблемы и выполняет действия владельца.

Telemax сохраняет принятую работу в SQLite до подтверждения доставки. Сообщение
либо доставлено, либо ждёт повтор, либо явно попадает в `failed`/`ambiguous`.
Exactly-once не обещается: если удалённая сторона приняла отправку, но ответ
потерялся, автоматический повтор мог бы создать дубль, поэтому решение остаётся
владельцу.

## Возможности

- текст, форматирование, ответы, правки и удаления в обе стороны;
- фото, видео, файлы, голосовые, видеозаметки, стикеры и альбомы;
- реакции, typing, read receipts и статус контакта;
- импорт истории и перенос собственных сообщений из MAX как опции;
- durable inbox/outbox, bounded retry, TTL и явное состояние неоднозначной
  доставки;
- один процесс и одна MAX-сессия для всех выбранных контактов;
- управление через Telegram без постоянного SSH-сеанса.

## Требования

- Linux с пользовательским `systemd`;
- Python 3.12 или новее;
- аккаунты MAX и Telegram;
- Telegram-бот-страж с включённым Bot Management Mode;
- `api_id` и `api_hash` Telegram для owner-session — их выдаёт
  [my.telegram.org](https://my.telegram.org/).

## Установка

```bash
git clone https://github.com/aipolukhin/telemax.git
cd telemax
python3.12 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e .
.venv/bin/telemax setup
```

`setup` проверит окружение, создаст локальные `config.yaml` и `.env`, установит
пользовательский systemd-unit и передаст дальнейшую настройку боту-стражу.
Первого стража нужно создать вручную в `@BotFather`: бот не может создать самого
себя.

Для сообщений, правок, удалений и реакций владельца нужна его Telegram
owner-session:

```bash
# Добавьте TELEMAX_API_ID и TELEMAX_API_HASH в .env, затем:
.venv/bin/telemax telegram-sync
```

Команда покажет QR-код. После авторизации перезапустите сервис из чата со
стражем или через `systemctl --user restart telemax.service`.

Проверка конфигурации:

```bash
.venv/bin/telemax validate-config
systemctl --user status telemax.service
```

## Конфигурация и секреты

Полный справочник с безопасными примерами находится в
[`config.example.yaml`](config.example.yaml), а перечень переменных — в
[`.env.example`](.env.example).

- `config.yaml` содержит локальные IDs и пути и не должен попадать в Git;
- `.env` содержит токены, номера и Telegram API credentials, создаётся с режимом
  `0600` и не должен копироваться в issue или логи;
- `data/` содержит SQLite, сессии и очередь доставки;
- путь к конфигу меняется через `--config` или `BRIDGE_CONFIG`;
- `BRIDGE_DATA_DIR` и `BRIDGE_LOG_LEVEL` переопределяют каталог данных и уровень
  логирования.

Для второй установки на том же сервере используйте отдельный конфиг и instance:

```bash
.venv/bin/telemax --config config-family.yaml setup --instance family
```

## Ограничения

- MAX не предоставляет стабильный публичный контракт для всех используемых
  возможностей, поэтому обновление сервиса или зависимости может потребовать
  совместимого релиза Telemax;
- Telegram Bot API ограничивает скачивание входящих файлов; превышение лимита
  показывается владельцу, а не теряется молча;
- native media автоматически деградирует в обычный файл/видео при явной
  несовместимости;
- удаление бота или сессии вне Telemax требует повторного подключения.

## Основа и благодарности

Telemax не реализует клиенты мессенджеров с нуля. Ключевые upstream-
проекты:

- [PyMax](https://github.com/MaxApiTeam/PyMax) (`maxapi-python`) — MAX-сессия,
  события, чаты и базовые операции; Telemax фиксирует проверенную версию;
- [aiogram](https://github.com/aiogram/aiogram) — Telegram Bot API, боты контактов и
  бот-страж;
- [Telethon](https://codeberg.org/Lonami/Telethon) — owner-session Telegram и MTProto;
- [aiohttp](https://github.com/aio-libs/aiohttp),
  [PyAV](https://github.com/PyAV-Org/PyAV) и
  [Pillow](https://github.com/python-pillow/Pillow) — HTTP и медиапайплайн.

Полный список прямых runtime-зависимостей, ссылок и лицензий — в
[`THIRD_PARTY.md`](THIRD_PARTY.md). Авторские права на эти проекты остаются у их
авторов и контрибьюторов.

## Разработка

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m ruff check bridge tests
.venv/bin/python -m mypy
.venv/bin/python -m pytest -q
```

Архитектура доставки описана в
[`docs/architecture/delivery-semantics.md`](docs/architecture/delivery-semantics.md),
схема БД — в
[`docs/architecture/database-schema.md`](docs/architecture/database-schema.md),
операционные действия — в [`docs/runbook.md`](docs/runbook.md), а ключевые
решения — в [`docs/adr/`](docs/adr/).

## Безопасность и лицензия

Уязвимости и утечки сообщайте по правилам из [`SECURITY.md`](SECURITY.md), не
публикуя токены, номера телефонов, содержимое сообщений или session-файлы.
Участие в разработке описано в [`CONTRIBUTING.md`](CONTRIBUTING.md).

Код распространяется по лицензии [Apache-2.0](LICENSE).
