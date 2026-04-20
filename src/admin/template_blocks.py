"""Реестр блоков и compile/decompose Jinja для issue-шаблонов уведомлений (админка)."""

from __future__ import annotations

import logging
import re
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from bot.template_loader import read_default_file

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SettingDef:
    type: Literal["emoji_select", "text"]
    default: str
    options: list[str] | None = None


@dataclass(frozen=True)
class BlockDef:
    id: str
    label: str
    description: str
    template: str
    variables: list[str]
    settings_schema: dict[str, SettingDef]
    signature_pattern: str
    default_enabled: bool
    order: int
    decompose_full_body: bool = False


@dataclass
class BlockConfig:
    block_id: str
    enabled: bool
    order: int
    settings: dict[str, str] = field(default_factory=dict)


def _normalize_for_match(s: str) -> str:
    s = re.sub(r"\s+", " ", s)
    s = s.replace('"', "'")
    return s.strip()


def _sanitize_setting_value(value: str) -> str:
    if re.search(r"\{[{%#]|[}%#]\}", value):
        raise ValueError(f"Setting value contains Jinja syntax: {value!r}")
    return value.replace("'", "\\'")


def apply_block_settings(block: BlockDef, settings: dict[str, str]) -> str:
    result = block.template
    for key, sdef in block.settings_schema.items():
        placeholder = f"__{block.id}_{key}__"
        count = result.count(placeholder)
        if count != 1:
            raise ValueError(f"Expected 1 placeholder {placeholder} in {block.id!r}, got {count}")
        value = settings.get(key, sdef.default)
        sanitized = _sanitize_setting_value(value)
        result = result.replace(placeholder, sanitized)
    return result


def compile_blocks_to_jinja(blocks: list[BlockConfig]) -> str:
    seen_ids: set[str] = set()
    parts: list[str] = []

    for bc in sorted(blocks, key=lambda b: b.order):
        if not bc.enabled:
            continue
        if bc.block_id in seen_ids:
            raise ValueError(f"Duplicate block_id: {bc.block_id}")
        seen_ids.add(bc.block_id)

        bdef = BLOCK_REGISTRY.get(bc.block_id)
        if bdef is None:
            raise ValueError(f"Unknown block_id: {bc.block_id}")

        fragment = apply_block_settings(bdef, bc.settings)
        parts.append(fragment)

    return "\n".join(parts)


def _allowed_block_defs(template_name: str) -> list[BlockDef]:
    cfg = DEFAULT_BLOCK_CONFIGS.get(template_name)
    if not cfg:
        return []
    order_map = {bc.block_id: bc.order for bc in cfg}
    out = [BLOCK_REGISTRY[bid] for bid in order_map if bid in BLOCK_REGISTRY]
    out.sort(key=lambda b: order_map[b.id])
    return out


def jinja_to_blocks(body_html: str, template_name: str) -> list[BlockConfig] | None:
    """Best-effort decompose. None если нераспознанный остаток."""
    if not body_html or not body_html.strip():
        return None

    allowed = _allowed_block_defs(template_name)
    if not allowed:
        return None

    if len(allowed) == 1 and allowed[0].decompose_full_body:
        ref = _normalize_for_match(compile_blocks_to_jinja(DEFAULT_BLOCK_CONFIGS[template_name]))
        if _normalize_for_match(body_html) == ref:
            return deepcopy(DEFAULT_BLOCK_CONFIGS[template_name])
        return None

    normalized = _normalize_for_match(body_html)

    matches: list[tuple[int, int, str, dict[str, str]]] = []

    for bdef in allowed:
        if bdef.decompose_full_body:
            continue
        pattern = bdef.signature_pattern.replace('"', "'")
        m = re.search(pattern, normalized)
        if m:
            settings: dict[str, str] = {}
            for key in bdef.settings_schema:
                group_name = f"{bdef.id}_{key}"
                try:
                    settings[key] = m.group(group_name)
                except IndexError:
                    settings[key] = bdef.settings_schema[key].default
            matches.append((m.start(), m.end(), bdef.id, settings))

    if not matches:
        return None

    covered = [False] * len(normalized)
    for start, end, _, _ in matches:
        for i in range(start, min(end, len(normalized))):
            covered[i] = True

    remainder = "".join(c for i, c in enumerate(normalized) if not covered[i])
    remainder_text = re.sub(r"<[^>]*>", "", remainder)
    remainder_text = remainder_text.strip()
    if remainder_text:
        logger.info(
            "decompose %s: unrecognized remainder (%d chars): %.100s",
            template_name,
            len(remainder_text),
            remainder_text,
        )
        return None

    matches.sort(key=lambda x: x[0])
    result: list[BlockConfig] = []
    for i, (_, _, block_id, settings) in enumerate(matches):
        result.append(BlockConfig(block_id=block_id, enabled=True, order=i, settings=settings))

    found_ids = {bc.block_id for bc in result}
    for bdef in allowed:
        if bdef.id not in found_ids:
            result.append(
                BlockConfig(
                    block_id=bdef.id,
                    enabled=False,
                    order=len(result),
                    settings={k: s.default for k, s in bdef.settings_schema.items()},
                )
            )

    return result


def verify_no_overlap(template_name: str) -> None:
    """Паттерны попарно не пересекаются в эталонном compiled output."""
    blocks = DEFAULT_BLOCK_CONFIGS[template_name]
    jinja = compile_blocks_to_jinja(blocks)
    normalized = _normalize_for_match(jinja)
    allowed_ids = {b.block_id for b in blocks}
    spans: list[tuple[int, int, str]] = []
    for bdef in _BLOCK_LIST:
        if bdef.id not in allowed_ids:
            continue
        if bdef.decompose_full_body:
            continue
        pattern = bdef.signature_pattern.replace('"', "'")
        m = re.search(pattern, normalized)
        if m:
            for prev_start, prev_end, prev_id in spans:
                overlap = m.start() < prev_end and m.end() > prev_start
                assert not overlap, (
                    f"Overlap in {template_name}: {bdef.id} [{m.start()}:{m.end()}] vs "
                    f"{prev_id} [{prev_start}:{prev_end}]"
                )
            spans.append((m.start(), m.end(), bdef.id))


# --- Block definitions (etalon = templates/bot/tpl_*.html.j2) ---

def _sig_new_issue_header() -> str:
    _q = r"['\"]"
    return (
        r"<p>\s*<strong>\s*\{\{\s*emoji\s*\|\s*default\s*\(\s*"
        + _q
        + r"(?P<new_issue_header_emoji>.*?)"
        + _q
        + r"\s*\)\s*\}\}\s*</strong>\s*Новая задача\s*"
        r'<a href="\{\{\s*issue_url\s*\|\s*default\s*\(\s*'
        + _q
        + r"(?:.*?)"
        + _q
        + r'\s*\)\s*\}\}">\#\{\{\s*issue_id\s*\|\s*default\s*\(\s*'
        + _q
        + r"(?:.*?)"
        + _q
        + r"\s*\)\s*\}\}</a></p>"
    )


def _sig_task_change_header() -> str:
    _q = r"['\"]"
    return (
        r"<p>\s*<strong>\s*\{\{\s*emoji\s*\|\s*default\s*\(\s*"
        + _q
        + r"(?P<task_change_header_emoji>.*?)"
        + _q
        + r"\s*\)\s*\}\}\s*</strong>\s*\{\{\s*title\s*\|\s*default\s*\(\s*"
        + _q
        + r"(?P<task_change_header_title>.*?)"
        + _q
        + r"\s*\)\s*\}\}\s*"
        r'<a href="\{\{\s*issue_url\s*\|\s*default\s*\(\s*'
        + _q
        + r"(?:.*?)"
        + _q
        + r'\s*\)\s*\}\}">\#\{\{\s*issue_id\s*\|\s*default\s*\(\s*'
        + _q
        + r"(?:.*?)"
        + _q
        + r"\s*\)\s*\}\}</a></p>"
    )


def _sig_reminder_header() -> str:
    _q = r"['\"]"
    return (
        r"<p>\s*<strong>(?P<reminder_header_emoji>.*?)</strong>\s*Напоминание по\s*"
        r'<a href="\{\{\s*issue_url\s*\|\s*default\s*\(\s*'
        + _q
        + r"(?:.*?)"
        + _q
        + r'\s*\)\s*\}\}">\#\{\{\s*issue_id\s*\|\s*default\s*\(\s*'
        + _q
        + r"(?:.*?)"
        + _q
        + r"\s*\)\s*\}\}</a></p>"
    )


def _sig_issue_subject() -> str:
    return r"<p>\{\{\s*subject\s*\|\s*default\s*\(\s*''\s*\)\s*\}\}</p>"


def _sig_new_issue_status() -> str:
    return (
        r"<p>Статус:\s*\{\{\s*status\s*\|\s*default\s*\(\s*''\s*\)\s*\}\}\s*·\s*Приоритет:\s*"
        r"\{\{\s*priority\s*\|\s*default\s*\(\s*''\s*\)\s*\}\}"
        r"(?:\s*\{%\s*if\s+version\s*\|\s*default\s*\(\s*''\s*\)\s*%\}\s*·\s*Версия:\s*"
        r"\{\{\s*version\s*\}\}\s*\{%\s*endif\s*%\})?</p>"
    )


def _sig_task_change_event() -> str:
    return (
        r"<p>Тип:\s*\{\{\s*event_type\s*\|\s*default\s*\(\s*''\s*\)\s*\}\}"
        r"(?:\s*\{%\s*if\s+extra_text\s*\|\s*default\s*\(\s*''\s*\)\s*%\}\s*—\s*"
        r"\{\{\s*extra_text\s*\}\}\s*\{%\s*endif\s*%\})?</p>"
    )


def _sig_reminder_text_block() -> str:
    _q = r"['\"]"
    return (
        r"<p>\{\{\s*reminder_text\s*\|\s*default\s*\(\s*"
        + _q
        + r"(?P<reminder_text_block_fallback>.*?)"
        + _q
        + r"\s*\)\s*\}\}</p>"
    )


_BLOCK_LIST: list[BlockDef] = [
    BlockDef(
        id="new_issue_header",
        label="Заголовок (новая задача)",
        description="Эмодзи и ссылка на задачу",
        template=(
            "<p><strong>{{ emoji | default('__new_issue_header_emoji__') }}</strong> "
            "Новая задача <a href=\"{{ issue_url | default('') }}\">"
            "#{{ issue_id | default('') }}</a></p>"
        ),
        variables=["emoji", "issue_url", "issue_id"],
        settings_schema={
            "emoji": SettingDef(
                type="emoji_select",
                default="🆕",
                options=["📋", "📝", "🔔", "⚠️", "✅", "❌", "🚀", "🐛", "💡", "📌", "🔧", "📊", "🆕"],
            ),
        },
        signature_pattern=_sig_new_issue_header(),
        default_enabled=True,
        order=0,
    ),
    BlockDef(
        id="task_change_header",
        label="Заголовок (изменение)",
        description="Эмодзи, заголовок и ссылка",
        template=(
            "<p><strong>{{ emoji | default('__task_change_header_emoji__') }}</strong> "
            "{{ title | default('__task_change_header_title__') }} "
            "<a href=\"{{ issue_url | default('') }}\">"
            "#{{ issue_id | default('') }}</a></p>"
        ),
        variables=["emoji", "title", "issue_url", "issue_id"],
        settings_schema={
            "emoji": SettingDef(
                type="emoji_select",
                default="📝",
                options=["📋", "📝", "🔔", "⚠️", "✅", "❌", "🚀", "🐛", "💡", "📌", "🔧", "📊", "🆕"],
            ),
            "title": SettingDef(type="text", default="Изменение"),
        },
        signature_pattern=_sig_task_change_header(),
        default_enabled=True,
        order=0,
    ),
    BlockDef(
        id="reminder_header",
        label="Заголовок (напоминание)",
        description="Эмодзи и ссылка",
        template=(
            "<p><strong>__reminder_header_emoji__</strong> Напоминание по "
            "<a href=\"{{ issue_url | default('') }}\">#{{ issue_id | default('') }}</a></p>"
        ),
        variables=["issue_url", "issue_id"],
        settings_schema={
            "emoji": SettingDef(
                type="emoji_select",
                default="⏰",
                options=["📋", "📝", "🔔", "⏰", "⚠️", "✅", "❌", "🚀", "🐛", "💡", "📌", "🔧", "📊"],
            ),
        },
        signature_pattern=_sig_reminder_header(),
        default_enabled=True,
        order=0,
    ),
    BlockDef(
        id="issue_subject",
        label="Тема",
        description="{{ subject }}",
        template="<p>{{ subject | default('') }}</p>",
        variables=["subject"],
        settings_schema={},
        signature_pattern=_sig_issue_subject(),
        default_enabled=True,
        order=1,
    ),
    BlockDef(
        id="new_issue_status",
        label="Статус и приоритет",
        description="Статус, приоритет, версия",
        template=(
            "<p>Статус: {{ status | default('') }} · Приоритет: {{ priority | default('') }}"
            "{% if version | default('') %} · Версия: {{ version }}{% endif %}</p>"
        ),
        variables=["status", "priority", "version"],
        settings_schema={},
        signature_pattern=_sig_new_issue_status(),
        default_enabled=True,
        order=2,
    ),
    BlockDef(
        id="task_change_event",
        label="Тип события",
        description="event_type и дополнительный текст",
        template=(
            "<p>Тип: {{ event_type | default('') }}"
            "{% if extra_text | default('') %} — {{ extra_text }}{% endif %}</p>"
        ),
        variables=["event_type", "extra_text"],
        settings_schema={},
        signature_pattern=_sig_task_change_event(),
        default_enabled=True,
        order=2,
    ),
    BlockDef(
        id="reminder_text_block",
        label="Текст напоминания",
        description="{{ reminder_text }}",
        template="<p>{{ reminder_text | default('__reminder_text_block_fallback__') }}</p>",
        variables=["reminder_text"],
        settings_schema={
            "fallback": SettingDef(type="text", default="Задача без движения"),
        },
        signature_pattern=_sig_reminder_text_block(),
        default_enabled=True,
        order=2,
    ),
    BlockDef(
        id="digest_body",
        label="Тело дайджеста",
        description="Список накопленных уведомлений",
        template=read_default_file("tpl_digest") or "",
        variables=["items"],
        settings_schema={},
        signature_pattern="",
        default_enabled=True,
        order=0,
        decompose_full_body=True,
    ),
    BlockDef(
        id="dry_run_body",
        label="Предпросмотр шаблона",
        description="Одна задача в контексте предпросмотра",
        template=read_default_file("tpl_dry_run") or "",
        variables=["issue_url", "issue_id", "subject"],
        settings_schema={},
        signature_pattern="",
        default_enabled=True,
        order=0,
        decompose_full_body=True,
    ),
]

BLOCK_REGISTRY: dict[str, BlockDef] = {b.id: b for b in _BLOCK_LIST}

DEFAULT_BLOCK_CONFIGS: dict[str, list[BlockConfig]] = {
    "tpl_new_issue": [
        BlockConfig("new_issue_header", True, 0, {"emoji": "🆕"}),
        BlockConfig("issue_subject", True, 1, {}),
        BlockConfig("new_issue_status", True, 2, {}),
    ],
    "tpl_task_change": [
        BlockConfig("task_change_header", True, 0, {"emoji": "📝", "title": "Изменение"}),
        BlockConfig("issue_subject", True, 1, {}),
        BlockConfig("task_change_event", True, 2, {}),
    ],
    "tpl_reminder": [
        BlockConfig("reminder_header", True, 0, {"emoji": "⏰"}),
        BlockConfig("issue_subject", True, 1, {}),
        BlockConfig("reminder_text_block", True, 2, {"fallback": "Задача без движения"}),
    ],
    "tpl_digest": [
        BlockConfig("digest_body", True, 0, {}),
    ],
    "tpl_dry_run": [
        BlockConfig("dry_run_body", True, 0, {}),
    ],
}

BLOCK_EDITOR_TEMPLATES: frozenset[str] = frozenset(DEFAULT_BLOCK_CONFIGS.keys())


def registry_json_objects() -> list[dict[str, Any]]:
    """Метаданные блоков для API и встраивания в HTML (один JSON на страницу)."""
    out: list[dict[str, Any]] = []
    for b in _BLOCK_LIST:
        out.append(
            {
                "id": b.id,
                "label": b.label,
                "description": b.description,
                "variables": list(b.variables),
                "settings_schema": {
                    k: {"type": s.type, "default": s.default, "options": s.options}
                    for k, s in b.settings_schema.items()
                },
                "default_enabled": b.default_enabled,
                "order": b.order,
            }
        )
    return out


def default_block_configs_as_dicts(template_name: str) -> list[dict[str, Any]]:
    rows = DEFAULT_BLOCK_CONFIGS.get(template_name, [])
    return [asdict(bc) for bc in rows]
