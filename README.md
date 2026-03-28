Вот обновлённый README, отражающий текущую модульную архитектуру. Вставь в `README.md`:

```markdown
# 🤖 Redmine → Matrix Notification Bot

Бот автоматически отслеживает изменения в задачах Redmine и отправляет уведомления в Matrix-чат (Element / Synapse / любой Matrix-клиент).

---

## Возможности

| № | Функция | Описание |
|---|---------|----------|
| 1 | **Новые задачи** | Уведомление при появлении новой назначенной задачи |
| 2 | **Смена статуса** | Уведомление при изменении статуса задачи |
| 3 | **Комментарии и изменения** | Отслеживание через Redmine journals API — автор и тип изменения |
| 4 | **Просроченные задачи** | Ежедневное уведомление с количеством дней просрочки |
| 5 | **Напоминания о дедлайне** | Уведомление, когда до срока задачи ≤ 3 дней |
| 6 | **Запрос информации** | Уведомление, когда заказчик предоставил информацию |
| 7 | **Маршрутизация** | Разные статусы / версии / команды → разные комнаты Matrix |
| 8 | **Мультипользовательность** | Поддержка нескольких пользователей с индивидуальными настройками |
| 9 | **Рабочие часы и DND** | Уведомления только в рабочее время; Emergency пробивает любой DND |

---

## Архитектура

```
┌──────────┐   REST API (каждые 30с)   ┌──────────────┐  Matrix C-S API   ┌──────────┐
│  Redmine │ ◄──────────────────────── │    src/      │ ────────────────► │  Matrix  │
│  (задачи)│                           │  APScheduler │                    │  (чат)   │
└──────────┘                           └──────┬───────┘                   └──────────┘
                                              │
                                       ┌──────▼──────┐
                                       │  data/      │
                                       │  JSON state │
                                       └─────────────┘
```

**Цикл работы:**
1. Планировщик вызывает проверку каждые 30 секунд
2. Для каждого пользователя загружаются назначенные задачи из Redmine
3. Текущее состояние сравнивается с сохранённым в state-файлах (`data/`)
4. `preferences` проверяет: рабочее ли время? включён ли DND? Emergency?
5. При обнаружении изменений — уведомление форматируется и отправляется в Matrix
6. Новое состояние сохраняется атомарно в state-файлы

---

## Стек технологий

| Компонент | Технология | Назначение |
|-----------|-----------|------------|
| Язык | Python 3.11+ | Основной язык |
| Matrix API | `matrix-nio` | Async-отправка HTML-сообщений в чат |
| Redmine API | `python-redmine` | Получение задач, журналов, статусов |
| Планировщик | `APScheduler` | Периодические проверки |
| Конфигурация | `python-dotenv` + `PyYAML` | Секреты из `.env`, маршруты из `config.yaml` |
| Тестирование | `pytest` + `pytest-asyncio` | 91 тест, 0.11 сек |
| Процесс-менеджер | systemd | Автозапуск, перезапуск при сбоях |

---

## Структура проекта

```
redmine-matrix-bot/
├── src/                            # Исходный код (модульная архитектура)
│   ├── __init__.py
│   ├── config.py                   # Конфигурация: .env, валидация, константы
│   ├── utils.py                    # Утилиты: время, safe_html, truncate
│   ├── state.py                    # Персистентное состояние (JSON, атомарная запись)
│   ├── preferences.py              # Рабочие часы, DND, can_notify()
│   ├── matrix_client.py            # Matrix: singleton-клиент, retry, send_message()
│   ├── commands.py                 # Обработчики команд бота
│   ├── onboarding.py               # Подключение новых пользователей
│   ├── redmine_checks.py           # Проверка изменений в задачах Redmine
│   ├── reports.py                  # Генерация отчётов
│   └── routing.py                  # Маршрутизация уведомлений по комнатам
│
├── tests/                          # Тесты (91 шт.)
│   ├── conftest.py                 # Фикстуры и sys.path
│   ├── test_config.py              # 15 тестов
│   ├── test_utils.py               # 25 тестов
│   ├── test_state.py               # 14 тестов
│   ├── test_preferences.py         # 27 тестов
│   └── test_matrix_client.py       # 10 тестов
│
├── data/                           # State-файлы JSON (gitignored)
├── config.yaml                     # Конфигурация пользователей и маршрутизации
├── .env                            # Секреты (токены) — НЕ в git!
├── .env.example                    # Шаблон .env для новых установок
├── requirements.txt                # Python-зависимости
├── pytest.ini                      # Настройки pytest (pythonpath = src)
├── README.md                       # Этот файл
├── redmine-matrix-bot.service      # Systemd unit-файл
└── .gitignore                      # Исключения из git
```

### Модули `src/` — краткое описание

| Модуль | Назначение | Тесты |
|--------|-----------|:-----:|
| `config.py` | Загрузка `.env`, пути, приоритеты, статусы Redmine, `validate_config()` | 15 |
| `utils.py` | `now()`, `safe_html()`, `truncate_text()`, timezone | 25 |
| `state.py` | `load_state()` / `save_state()` — JSON с атомарной записью | 14 |
| `preferences.py` | Рабочие часы, DND, `can_notify()` с Emergency bypass | 27 |
| `matrix_client.py` | Singleton `AsyncClient`, access_token, retry × 3, `send_message()` | 10 |
| `redmine_checks.py` | Основная логика проверки задач | — |
| `routing.py` | Выбор комнаты Matrix по статусу/версии задачи | — |
| `commands.py` | Интерактивные команды бота | — |
| `onboarding.py` | Регистрация новых пользователей | — |
| `reports.py` | Генерация отчётов | — |

---

## Быстрый старт

### 1. Клонирование и окружение

```bash
git clone git@github.com:forgebeard/redmine-matrix-bot.git
cd redmine-matrix-bot

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Настройка секретов

```bash
cp .env.example .env
nano .env   # заполнить все переменные (см. раздел «Настройка .env»)
```

### 3. Настройка пользователей и маршрутизации

```bash
nano config.yaml   # настроить пользователей (см. раздел «Настройка config.yaml»)
```

### 4. Проверка конфигурации

```bash
cd src && python3 -c "from config import validate_config; ok, m = validate_config(); print('✅ OK' if ok else f'❌ Не хватает: {m}')"
```

### 5. Запуск тестов

```bash
python -m pytest tests/ -v --tb=short
# Ожидается: 91 passed
```

### 6. Тестовый запуск бота

```bash
python3 src/bot.py
```

Если в логе видно `✅ Matrix: ...` и `✅ Redmine: ...` — бот работает. Остановить — `Ctrl+C`.

### 7. Установка как systemd-сервис

```bash
nano redmine-matrix-bot.service   # указать пути и пользователя

sudo cp redmine-matrix-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable redmine-matrix-bot
sudo systemctl start redmine-matrix-bot
```

---

## Настройка .env

```env
# ─── Matrix-сервер ───────────────────────────────────
MATRIX_HOMESERVER=https://messenger.example.com
MATRIX_ACCESS_TOKEN=syt_your_access_token_here
MATRIX_USER_ID=@bot_user:messenger.example.com
MATRIX_DEVICE_ID=BOTDEVICE

# ─── Redmine ─────────────────────────────────────────
REDMINE_URL=https://redmine.example.com
REDMINE_API_KEY=your_redmine_api_key
```

### Где взять токены

| Параметр | Где получить |
|----------|-------------|
| `MATRIX_ACCESS_TOKEN` | Element → Настройки → Помощь и О программе → Access Token |
| `MATRIX_USER_ID` | Формат: `@username:your.server` |
| `MATRIX_DEVICE_ID` | Любой идентификатор (например, `BOTDEVICE`) |
| `REDMINE_API_KEY` | Redmine → Моя учётная запись → API-ключ (правая колонка) |

> ⚠️ **Важно:** Бот использует `access_token` (не пароль) для авторизации в Matrix — это безопаснее и не требует login-flow.

> ⚠️ **Важно:** API-ключ Redmine определяет, чьи задачи бот может видеть. Рекомендуется использовать ключ с правами администратора, если бот обслуживает нескольких пользователей.

---

## Настройка config.yaml

Файл определяет **пользователей**, **типы уведомлений** и **маршрутизацию** в комнаты Matrix.

```yaml
users:
  # ─── Пользователь 1: получает ВСЕ уведомления в одну комнату ───
  1972:                                              # Redmine user ID
    matrix_user: "@user1:messenger.example.com"      # Matrix-аккаунт пользователя
    default_room: "!room_id:messenger.example.com"   # Комната по умолчанию
    notify: ["all"]                                   # Все типы уведомлений

  # ─── Пользователь 2: выборочные уведомления + маршрутизация ───
  3254:                                              # Redmine user ID
    matrix_user: "@user2:messenger.example.com"
    default_room: "!room_id2:messenger.example.com"
    notify: ["new", "info", "issue_updated"]          # Только выбранные типы

    # Маршрутизация по статусу задачи:
    status_routes:
      - status: "Передано в работу"
        room: "!team_room:messenger.example.com"

    # Маршрутизация по версии (продукту):
    version_routes:
      - match: "Продукт А"
        room: "!product_a_room:messenger.example.com"
      - match: "Продукт Б"
        room: "!product_b_room:messenger.example.com"

    # Комната команды (опционально)
    team_room: "!team_room:messenger.example.com"
```

### Как узнать Redmine user ID

Откройте профиль пользователя в Redmine — ID будет в URL:
```
https://redmine.example.com/users/1972
                                 ^^^^
```

### Как узнать Matrix Room ID

В Matrix-клиенте (Element / др.): Настройки комнаты → Дополнительно → Внутренний ID комнаты.

Формат: `!случайная_строка:ваш.сервер`

### Типы уведомлений (notify)

| Тип | Описание |
|-----|----------|
| `all` | Все типы сразу (шорткат) |
| `new` | Новая задача появилась в списке назначенных |
| `status_change` | Статус задачи изменился |
| `issue_updated` | Новый комментарий или изменение в журнале задачи |
| `info` | Статус задачи = «Запрос информации» |
| `overdue` | Дедлайн задачи прошёл (уведомление раз в сутки) |
| `reminder` | До дедлайна ≤ 3 дней (уведомление раз в сутки) |

### Приоритет маршрутизации

Бот выбирает комнату в следующем порядке (первое совпадение):

1. **status_routes** — если статус задачи совпадает
2. **version_routes** — если версия задачи содержит подстроку
3. **team_room** — комната команды
4. **default_room** — комната по умолчанию (fallback)

---

## State-файлы

Бот хранит состояние в JSON-файлах в директории `data/` (вместо БД — для простоты и переносимости):

| Файл | Назначение |
|------|-----------|
| `data/state_{uid}_sent.json` | Задачи, о которых уже уведомили + их текущий статус |
| `data/state_{uid}_journals.json` | Последний `journal_id` — для отслеживания новых изменений |
| `data/state_{uid}_overdue.json` | Дата последнего уведомления о просрочке |
| `data/state_{uid}_reminders.json` | Дата последнего напоминания о дедлайне |

`{uid}` — Redmine user ID из `config.yaml`.

Директория `data/` создаётся автоматически при первом запуске. Запись файлов — **атомарная** (через temp-файл + rename), что предотвращает повреждение данных при внезапном отключении.

> 💡 При **первом запуске** бот НЕ отправляет уведомления — только запоминает текущее состояние всех задач. Это предотвращает спам при начальной инициализации.

### Сброс состояния

```bash
# Полный сброс — бот заново запомнит состояние (без спама при первом цикле)
rm -f data/state_*.json
sudo systemctl restart redmine-matrix-bot

# Сброс только journals — при следующем цикле обработает все журналы как новые
rm -f data/state_*_journals.json
sudo systemctl restart redmine-matrix-bot
```

---

## Рабочие часы и DND

Бот поддерживает режим рабочего времени и «Не беспокоить»:

| Параметр | По умолчанию | Описание |
|----------|:------------:|----------|
| Рабочие часы | 09:00–18:00 | Уведомления отправляются только в этот интервал |
| Рабочие дни | Пн–Пт | Выходные = тишина |
| DND | выкл. | Пользователь может включить вручную |
| Emergency | всегда | Приоритет «Немедленный» пробивает любой DND и нерабочее время |

---

## Формат уведомлений

Все уведомления отправляются в HTML (`org.matrix.custom.html`) с кликабельными ссылками. Спецсимволы в данных из Redmine автоматически экранируются (`safe_html`).

### 🆕 Новая задача

```
🆕 Новая задача

#63603 — Настройка сервера

Статус: Новая
Приоритет: Нормальный
Версия: Продукт 2.0
📅 Срок: 2026-04-01

🔗 Открыть задачу
```

### 📝 Задача обновлена

```
📝 Задача обновлена

#63603 — Настройка сервера

Статус: Информация предоставлена
Приоритет: Нормальный
Версия: Продукт 2.0

💬 Комментарий от Иванов Иван
Смена статуса: В работе → Информация предоставлена

🔗 Открыть задачу
```

### ⚠️ Просроченная задача

```
⚠️ Просроченная задача

#55339 — Обновление документации

Статус: В работе
Приоритет: Низкий
📅 Срок: 2025-12-19 (просрочено на 98 дней)

🔗 Открыть задачу
```

### 🔄 Смена статуса

```
🔄 Статус изменён

#63603 — Настройка сервера

Статус: Информация предоставлена (было: В работе)
Приоритет: Нормальный

🔗 Открыть задачу
```

### ⏰ Напоминание о дедлайне

```
⏰ Скоро дедлайн

#63603 — Настройка сервера

Статус: В работе
📅 Срок: 2026-03-30 (через 3 дня)

🔗 Открыть задачу
```

### Сводная таблица типов

| Эмодзи | Тип | Когда срабатывает |
|--------|-----|-------------------|
| 🆕 | Новая задача | Задача появилась в списке назначенных |
| 🔄 | Смена статуса | Статус задачи изменился |
| 📝 | Обновление | Новый комментарий или изменение в журнале |
| ⚠️ | Просрочка | `due_date` < сегодня (раз в сутки) |
| ⏰ | Напоминание | До `due_date` ≤ 3 дней (раз в сутки) |
| ℹ️ | Запрос информации | Статус = «Запрос информации» |

---

## Тестирование

Проект покрыт **91 тестом** (время прогона ~0.11 сек):

```bash
# Все тесты
python -m pytest tests/ -v --tb=short

# Один модуль
python -m pytest tests/test_preferences.py -v

# С покрытием (если установлен pytest-cov)
python -m pytest tests/ --cov=src --cov-report=term-missing
```

| Модуль | Тестов | Что проверяется |
|--------|:------:|----------------|
| `config.py` | 15 | Загрузка env, валидация, приоритеты, маппинг |
| `utils.py` | 25 | Timezone, HTML-экранирование, truncate, edge cases |
| `state.py` | 14 | JSON load/save, атомарная запись, битые данные |
| `preferences.py` | 27 | Рабочие часы, DND, Emergency bypass |
| `matrix_client.py` | 10 | Singleton, retry, send, моки nio |

---

## Systemd-сервис

### Установка

Отредактируйте файл `redmine-matrix-bot.service` — укажите правильные пути и имя пользователя:

```ini
[Unit]
Description=Redmine Matrix Notification Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=your_user
Group=your_user
WorkingDirectory=/path/to/redmine-matrix-bot
ExecStart=/path/to/redmine-matrix-bot/venv/bin/python3 src/bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

### Управление

```bash
sudo systemctl start redmine-matrix-bot     # Запуск
sudo systemctl stop redmine-matrix-bot      # Остановка
sudo systemctl restart redmine-matrix-bot   # Перезапуск
sudo systemctl status redmine-matrix-bot    # Статус
sudo systemctl enable redmine-matrix-bot    # Автозапуск при загрузке ОС
```

---

## Мониторинг

```bash
# Лог в реальном времени
tail -f bot.log

# Последние 50 строк
tail -50 bot.log

# Посмотреть state-файлы
python3 -m json.tool data/state_*_sent.json | head -30

# Проверка конфигурации
cd src && python3 -c "from config import validate_config; print(validate_config())"
```

### Пример лога (штатная работа)

```log
2025-07-14 16:39:40 [INFO] 🔍 Проверка в 16:39:40...
2025-07-14 16:39:47 [INFO] 👤 User 1972: 32 задач
2025-07-14 16:39:48 [INFO] 📨 #63603 → !room_id... (status_change)
2025-07-14 16:40:26 [INFO] 👤 User 3254: 29 задач
2025-07-14 16:40:45 [INFO] ✅ Проверка завершена
```

---

## Безопасность

| Аспект | Решение |
|--------|---------|
| Секреты | Хранятся в `.env`, не в коде |
| Авторизация Matrix | `access_token` (не пароль) |
| Git | `.env`, `data/`, `*.log` в `.gitignore` |
| Запись файлов | Атомарная (`tempfile` + `os.replace`) |
| Логи | Не содержат токенов и API-ключей |
| HTML-инъекции | `safe_html()` экранирует данные из Redmine |
| Systemd | Запуск от непривилегированного пользователя |
| Redmine API | Только чтение (`assigned_to_id=me`) |

---

## Решение проблем

### Бот не запускается

```bash
# Проверить конфигурацию
cd src && python3 -c "from config import validate_config; ok, m = validate_config(); print('OK' if ok else m)"

# Проверить зависимости
source venv/bin/activate
pip install -r requirements.txt

# Запустить вручную и посмотреть ошибки
python3 src/bot.py 2>&1 | head -30
```

### Бот не отправляет уведомления

1. Проверьте `MATRIX_ACCESS_TOKEN` — токен может быть просрочен
2. Проверьте, что бот присоединён к комнатам Matrix (пригласите его)
3. Проверьте `REDMINE_API_KEY` — ключ должен иметь доступ к задачам
4. Проверьте Redmine user ID в `config.yaml` — ID в URL профиля
5. Посмотрите лог: `tail -50 bot.log`

### Бот спамит старыми уведомлениями после перезапуска

Удалите state-файлы — бот заново проинициализирует состояние:

```bash
rm -f data/state_*.json
sudo systemctl restart redmine-matrix-bot
```

### Matrix: «не удалось отправить»

- Бот повторяет отправку до 3 раз с паузой 2 сек
- Проверьте, что бот-пользователь создан и приглашён в комнаты
- Проверьте `MATRIX_HOMESERVER` — URL должен быть доступен с сервера бота

### Тесты падают

```bash
# Убедитесь что запускаете из корня проекта
cd /path/to/redmine-matrix-bot
python -m pytest tests/ -v --tb=long
```

---

## Лицензия

MIT