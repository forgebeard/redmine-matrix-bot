# 🤖 Redmine → Matrix Notification Bot

Бот автоматически отслеживает изменения в задачах Redmine и отправляет
уведомления в Matrix-чат (Element / РЕД V / Synapse).

## Возможности

| № | Функция | Описание |
|---|---------|----------|
| 1 | **Новые задачи** | Уведомление при появлении задачи со статусом «Новая» |
| 2 | **Информация предоставлена** | Уведомление + повторное напоминание каждый час |
| 3 | **Просроченные задачи** | Ежедневное уведомление с количеством дней просрочки |
| 4 | **Смена статуса** | Уведомление при изменении статуса (например «Новая» → «В работе») |
| 5 | **Комментарии и изменения** | Отслеживание через journals API — автор + тип изменения |
| 6 | **Маршрутизация по статусу** | Разные статусы → разные комнаты Matrix (настраивается в `.env`) |
| 7 | **Общая комната команды** | Дублирование новых задач определённого проекта в командную комнату |
| 8 | **Утренний отчёт** | Ежедневно в 09:00 — сводка по задачам, просрочкам, ожиданиям |
| 9 | **Автоочистка** | Ежедневно в 03:00 — удаление записей о закрытых задачах из state-файлов |

## Архитектура

┌──────────┐    API (каждые 5 мин)    ┌─────────────┐   room_send()    ┌──────────┐
│  Redmine │ ◄──────────────────────── │  Бот (Python)│ ──────────────► │  Matrix  │
│  (задачи)│                           │  APScheduler │                  │  (чат)   │
└──────────┘                           └──────┬──────┘                  └──────────┘
│
┌──────▼──────┐
│ JSON state  │
│   файлы     │
└─────────────┘


## Стек технологий

| Компонент | Технология | Назначение |
|-----------|-----------|------------|
| Язык | Python 3.11+ | Основной язык |
| Matrix API | matrix-nio | Отправка HTML-сообщений в чат |
| Redmine API | python-redmine | Получение задач, журналов, статусов |
| Планировщик | APScheduler | Периодические проверки и отчёты |
| Конфигурация | python-dotenv | Загрузка секретов из `.env` |
| Процесс-менеджер | systemd | Автозапуск, перезапуск при сбоях |

## Структура проекта

matrix_bot_firebeard/
├── bot.py                  # Основной код бота
├── .env                    # Секреты и настройки (НЕ в git!)
├── .env.example            # Шаблон .env для новых установок
├── requirements.txt        # Python-зависимости
├── README.md               # Этот файл
├── redmine-matrix-bot.service  # Systemd unit-файл
├── venv/                   # Виртуальное окружение Python
│
├── sent_issues.json        # State: уведомлённые задачи + статусы
├── reminders.json          # State: время последних напоминаний
├── overdue_issues.json     # State: даты уведомлений о просрочках
├── journals.json           # State: последний journal_id для задач
└── bot.log                 # Лог (ротация: 5 МБ × 5 файлов)


## Быстрый старт

### 1. Клонирование и окружение

```bash
cd ~/Документы/projects
git clone <repo_url> matrix_bot_firebeard
cd matrix_bot_firebeard

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

### 2. Настройка

cp .env.example .env
nano .env   # заполнить все переменные

### 3. Тестовый запуск

python3 bot.py

### 4. Установка как systemd-сервис

sudo cp redmine-matrix-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable redmine-matrix-bot
sudo systemctl start redmine-matrix-bot

### Настройка .env

# Matrix-сервер
MATRIX_HOMESERVER=https://messenger.example.com
MATRIX_ACCESS_TOKEN=syt_xxxxx...
MATRIX_USER_ID=@username:messenger.example.com
MATRIX_DEVICE_ID=bot_device

# Комнаты
MATRIX_ROOM_ID=!personal_room:server           # Личная (обязательно)
MATRIX_TEAM_ROOM_ID=                            # Командная (опционально)

# Маршрутизация: статус → дополнительная комната
# Личная комната ВСЕГДА получает уведомления
# Задачи с указанными статусами ДОПОЛНИТЕЛЬНО идут в указанную комнату
STATUS_ROOM_MAP={"Передано в работу.РВ": "!room:server"}

# Redmine
REDMINE_URL=https://redmine.example.com
REDMINE_API_KEY=xxxxx...

# Таймзона
BOT_TIMEZONE=Asia/Irkutsk

### Где взять токены

Параметр	Где получить
MATRIX_ACCESS_TOKEN	Ред V → Настройки → Помощь и О программе → Токен доступа
MATRIX_ROOM_ID	Ред V → Комната → Настройки → Дополнительно → Внутренний ID
REDMINE_API_KEY	Redmine → Моя учётная запись → API-ключ (правая колонка)

### Маршрутизация по статусам
Задачи с разными статусами могут отправляться в разные комнаты Matrix.

Настраивается через STATUS_ROOM_MAP в .env:

STATUS_ROOM_MAP={"Передано в работу.РВ": "!abc:server", "На согласовании": "!def:server"}

Логика:

Личная комната (MATRIX_ROOM_ID) — получает все уведомления всегда
Комната из STATUS_ROOM_MAP — получает уведомления дополнительно для задач с указанным статусом
Командная комната (MATRIX_TEAM_ROOM_ID) — получает новые задачи определённого проекта
Добавление нового маршрута — только правка .env, без изменения кода.

## State-файлы

Бот хранит состояние в JSON-файлах (вместо БД — для простоты):

Файл	Назначение
sent_issues.json	Задачи, о которых уже уведомили + их текущий статус
reminders.json	Время последнего напоминания для каждой задачи
overdue_issues.json	Дата последнего уведомления о просрочке
journals.json	Последний journal_id — для отслеживания новых изменений

Файлы пишутся атомарно (.tmp → rename). Очищаются автоматически в 03:00.

### Сброс состояния

# Полный сброс — бот заново уведомит обо всех задачах
rm -f sent_issues.json reminders.json overdue_issues.json journals.json
sudo systemctl restart redmine-matrix-bot

# Сброс только journals — уведомит о всех комментариях заново
rm -f journals.json
sudo systemctl restart redmine-matrix-bot

### Расписание

Задача	Когда	Описание
check_issues()	Каждые 5 минут	Основная проверка задач Redmine
daily_report()	09:00 ежедневно	Утренний отчёт в личную комнату
cleanup_state_files()	03:00 ежедневно	Очистка state от закрытых задач

### Systemd-сервис

# /etc/systemd/system/redmine-matrix-bot.service
[Unit]
Description=Redmine Matrix Notification Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=redadmin
Group=redadmin
WorkingDirectory=/home/redadmin/Документы/projects/matrix_bot_firebeard
ExecStart=/home/redadmin/Документы/projects/matrix_bot_firebeard/venv/bin/python3 bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target

Управление

sudo systemctl start redmine-matrix-bot     # Запуск
sudo systemctl stop redmine-matrix-bot      # Остановка
sudo systemctl restart redmine-matrix-bot   # Перезапуск
sudo systemctl status redmine-matrix-bot    # Статус
sudo systemctl enable redmine-matrix-bot    # Автозапуск при загрузке ОС

Мониторинг

# Лог в реальном времени
tail -f ~/Документы/projects/matrix_bot_firebeard/bot.log

# Последние 50 строк
tail -50 ~/Документы/projects/matrix_bot_firebeard/bot.log

# Посмотреть state-файлы
cat sent_issues.json | python3 -m json.tool
cat journals.json | python3 -m json.tool

Безопасность

Аспект	Решение
Секреты	Хранятся в .env, не в коде
Git	.env в .gitignore
Запись файлов	Атомарная (.tmp + rename)
Логи	Не содержат токенов и API-ключей
Systemd	Запуск от непривилегированного пользователя
Redmine API	Только чтение (assigned_to_id=me)

Формат уведомлений
Все уведомления в HTML (org.matrix.custom.html) с кликабельными ссылками:

🆕 Новая задача

#63603 — Настройка сервера
Проект — Служба техподдержки
Статус: Новая
Приоритет: Нормальный
Срок: 2026-04-01
🔗 Открыть задачу

Типы уведомлений
Эмодзи	Тип	Когда срабатывает
🆕	Новая задача	Задача со статусом «Новая» или маппированным статусом
✅	Информация предоставлена	Заказчик ответил — ждёт реакции инженера
⏰	Напоминание	Повтор каждый час для «Информация предоставлена»
⚠️	Просрочка	due_date < сегодня (раз в сутки)
🔄	Смена статуса	Статус изменился (например «Новая» → «В работе»)
📝	Обновление	Новый комментарий или изменение полей задачи

