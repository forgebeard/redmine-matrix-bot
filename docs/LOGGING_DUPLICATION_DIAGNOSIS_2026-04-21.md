# Logging duplication diagnosis (2026-04-21)

## Scope

Investigation of repeated bot log lines in Docker runtime.

## Findings

1. `redmine_bot` handlers were added in `src/bot/main.py` unconditionally:
   - file handler via `logger.addHandler(_fh)`
   - console handler via `logger.addHandler(_ch)`
2. `setup_json_logging("redmine_bot")` may already create a stream handler when `WANT_JSON_LOG=1`.
3. With mixed entry/import paths (`src.bot.main` and `bot.main`), logger setup could run multiple times, attaching duplicate handlers.
4. `logger.propagate` was left default (`True`), allowing duplication through parent/root handlers in some container setups.

## Reproduction checklist

- Start bot in Docker and inspect logs for pair-duplicated lines:
  - `Unassigned NEW: старт проверки`
  - `journal_contract_check ...`
  - `Журнальный цикл завершён ...`
- In runtime shell, inspect handler count for `logging.getLogger("redmine_bot").handlers`.

## Fix strategy adopted

- Make handler setup idempotent in `src/bot/main.py`:
  - do not add duplicate stream handlers,
  - do not add duplicate file handlers for the same path.
- Set `logger.propagate = False` for `redmine_bot` to prevent root propagation duplicates.

## Verification targets

- Each bot event appears once in Docker logs.
- File log (`data/bot.log`) has no pair duplication for the same timestamp/message.
