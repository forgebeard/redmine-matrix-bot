# SQL baseline — Via notifications

Date: 2026-04-21  
Source: runtime Postgres in docker compose (`via-postgres-1`)  
Method: `docker compose exec -T postgres psql -U bot -d via`

## 1) Row counts (config, routes, queues, templates)

```sql
SELECT 'bot_users' AS table_name, count(*) AS rows FROM bot_users
UNION ALL SELECT 'support_groups', count(*) FROM support_groups
UNION ALL SELECT 'status_room_routes', count(*) FROM status_room_routes
UNION ALL SELECT 'version_room_routes', count(*) FROM version_room_routes
UNION ALL SELECT 'user_version_routes', count(*) FROM user_version_routes
UNION ALL SELECT 'group_version_routes', count(*) FROM group_version_routes
UNION ALL SELECT 'cycle_settings', count(*) FROM cycle_settings
UNION ALL SELECT 'pending_digests', count(*) FROM pending_digests
UNION ALL SELECT 'pending_notifications', count(*) FROM pending_notifications
UNION ALL SELECT 'notification_templates', count(*) FROM notification_templates
ORDER BY table_name;
```

| table_name | rows |
|---|---:|
| bot_users | 1 |
| cycle_settings | 15 |
| group_version_routes | 0 |
| notification_templates | 1 |
| pending_digests | 0 |
| pending_notifications | 0 |
| status_room_routes | 0 |
| support_groups | 2 |
| user_version_routes | 0 |
| version_room_routes | 0 |

## 2) Room prefix distribution (`@`, `!`, other)

```sql
WITH all_rooms AS (
  SELECT 'bot_users.room' AS src, room AS room FROM bot_users
  UNION ALL SELECT 'support_groups.room_id', room_id FROM support_groups
  UNION ALL SELECT 'status_room_routes.room_id', room_id FROM status_room_routes
  UNION ALL SELECT 'version_room_routes.room_id', room_id FROM version_room_routes
  UNION ALL SELECT 'user_version_routes.room_id', room_id FROM user_version_routes
  UNION ALL SELECT 'group_version_routes.room_id', room_id FROM group_version_routes
)
SELECT
  src,
  CASE
    WHEN room LIKE '@%' THEN '@mxid'
    WHEN room LIKE '!%' THEN '!room'
    ELSE 'other'
  END AS room_kind,
  count(*) AS cnt
FROM all_rooms
GROUP BY src, room_kind
ORDER BY src, room_kind;
```

| src | room_kind | cnt |
|---|---|---:|
| bot_users.room | @mxid | 1 |
| support_groups.room_id | !room | 1 |
| support_groups.room_id | other | 1 |

## 3) `notify` token format classification

```sql
WITH tokens AS (
  SELECT 'bot_users' AS src, jsonb_array_elements_text(notify) AS token FROM bot_users
  UNION ALL
  SELECT 'support_groups', jsonb_array_elements_text(notify) FROM support_groups
),
norm AS (
  SELECT src, lower(trim(token)) AS token FROM tokens
)
SELECT
  src,
  CASE
    WHEN token='all' THEN 'all'
    WHEN token ~ '^[0-9]+$' THEN 'numeric_status_id_or_code'
    WHEN token IN ('new','reopened','info','reminder','overdue','issue_updated','status_change','daily_report')
      THEN 'notification_type_key'
    ELSE 'other_text'
  END AS token_kind,
  count(*) AS cnt
FROM norm
GROUP BY src, token_kind
ORDER BY src, token_kind;
```

| src | token_kind | cnt |
|---|---|---:|
| bot_users | numeric_status_id_or_code | 3 |
| support_groups | all | 1 |
| support_groups | numeric_status_id_or_code | 2 |

## 4) Config snapshots (sample rows)

### bot_users

```sql
SELECT
  id, redmine_id, display_name, group_id, room,
  notify::text AS notify, versions::text AS versions, priorities::text AS priorities,
  timezone, work_hours, work_days::text AS work_days, dnd
FROM bot_users
ORDER BY id;
```

| id | redmine_id | display_name | group_id | room | notify | versions | priorities | timezone | work_hours | work_days | dnd |
|---:|---:|---|---:|---|---|---|---|---|---|---|---|
| 3 | 1972 | Меренков Дмитрий | 2 | @dmitry.merenkov:messenger.red-soft.ru | ["13", "5", "18"] | ["12", "7"] | ["1", "2", "3", "4"] | Asia/Irkutsk | 09:00-18:00 | [0, 1, 2, 3, 4] | false |

### support_groups

```sql
SELECT
  id, name, room_id, notify_on_assignment, is_active, timezone,
  notify::text AS notify, versions::text AS versions, priorities::text AS priorities,
  work_hours, work_days::text AS work_days, dnd
FROM support_groups
ORDER BY id;
```

| id | name | room_id | notify_on_assignment | is_active | timezone | notify | versions | priorities | work_hours | work_days | dnd |
|---:|---|---|---|---|---|---|---|---|---|---|---|
| 1 | UNASSIGNED |  | true | true |  | ["all"] | ["all"] | ["all"] |  |  | false |
| 2 | ТП РЕД ВИРТ | !XcxsXTKOdhcXgmTQsu:messenger.red-soft.ru | true | true | Europe/Moscow | ["1", "22"] | ["12", "7"] | ["1", "2", "3", "4"] | 09:00-18:00 | [0, 1, 2, 3, 4] | false |

## 5) Routes coverage

```sql
SELECT 'status_room_routes' AS route_table, count(*) AS rows FROM status_room_routes
UNION ALL SELECT 'version_room_routes', count(*) FROM version_room_routes
UNION ALL SELECT 'user_version_routes', count(*) FROM user_version_routes
UNION ALL SELECT 'group_version_routes', count(*) FROM group_version_routes;
```

| route_table | rows |
|---|---:|
| status_room_routes | 0 |
| version_room_routes | 0 |
| user_version_routes | 0 |
| group_version_routes | 0 |

## 6) Cycle settings

```sql
SELECT key, value
FROM cycle_settings
ORDER BY key;
```

| key | value |
|---|---|
| BOT_TIMEZONE | Europe/Moscow |
| DEFAULT_REMINDER_INTERVAL | 14400 |
| DLQ_BATCH_SIZE | 10 |
| DRAIN_MAX_USERS_PER_TICK | 5 |
| JOURNAL_ENGINE_ENABLED | 0 |
| LAST_ISSUES_POLL_AT |  |
| MATRIX_MAX_RPS | 5 |
| MAX_DLQ_RETRIES | 5 |
| MAX_ISSUES_PER_TICK | 50 |
| MAX_PAGES_PER_TICK | 3 |
| MAX_REMINDERS | 3 |
| WATCHER_CACHE_REFRESH_EVERY_N_TICKS | 10 |
| check_interval | 90 |
| group_repeat_seconds | 1800 |
| reminder_after | 3600 |

## 7) Digest / DLQ queue state

```sql
SELECT 'pending_digests' AS queue, count(*) AS rows, min(created_at) AS oldest, max(created_at) AS newest
FROM pending_digests
UNION ALL
SELECT 'pending_notifications', count(*), min(created_at), max(created_at)
FROM pending_notifications;
```

| queue | rows | oldest | newest |
|---|---:|---|---|
| pending_digests | 0 |  |  |
| pending_notifications | 0 |  |  |

## 8) Template overrides state

```sql
SELECT
  id,
  name,
  (body_html IS NOT NULL AND length(body_html) > 0) AS has_body_html,
  (body_plain IS NOT NULL AND length(body_plain) > 0) AS has_body_plain,
  updated_by,
  updated_at
FROM notification_templates
ORDER BY name;
```

| id | name | has_body_html | has_body_plain | updated_by | updated_at |
|---:|---|---|---|---|---|
| 1 | tpl_new_issue | false | false | admin | 2026-04-20 16:46:28.39823+00 |

