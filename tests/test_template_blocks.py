"""Unit-тесты admin.template_blocks."""

from __future__ import annotations

import re

import pytest

from admin.template_blocks import (
    _BLOCK_LIST,
    ARCHIVED_TEMPLATE_BLOCKS,
    BLOCK_EDITOR_TEMPLATES,
    BLOCK_REGISTRY,
    DEFAULT_BLOCK_CONFIGS,
    BlockConfig,
    _normalize_for_match,
    _sanitize_setting_value,
    apply_block_settings,
    compile_blocks_to_jinja,
    jinja_to_blocks,
    prepare_body_html_for_decompose,
    registry_json_objects,
    verify_no_overlap,
)
from bot.template_loader import read_default_file


class TestBlockRegistry:
    def test_module_marked_archived(self) -> None:
        assert ARCHIVED_TEMPLATE_BLOCKS is True

    def test_unique_ids(self) -> None:
        ids = [b.id for b in _BLOCK_LIST]
        assert len(ids) == len(set(ids))

    def test_all_have_signature_pattern(self) -> None:
        for b in _BLOCK_LIST:
            if b.decompose_full_body:
                continue
            assert b.signature_pattern
            re.compile(b.signature_pattern)

    def test_placeholders_present(self) -> None:
        for b in _BLOCK_LIST:
            for key in b.settings_schema:
                placeholder = f"__{b.id}_{key}__"
                assert placeholder in b.template

    def test_placeholder_count_is_one(self) -> None:
        for b in _BLOCK_LIST:
            for key in b.settings_schema:
                placeholder = f"__{b.id}_{key}__"
                assert b.template.count(placeholder) == 1

    def test_default_configs_reference_valid_blocks(self) -> None:
        for tpl_name, configs in DEFAULT_BLOCK_CONFIGS.items():
            for bc in configs:
                assert bc.block_id in BLOCK_REGISTRY

    def test_editor_templates_match_defaults(self) -> None:
        assert frozenset(DEFAULT_BLOCK_CONFIGS.keys()) == BLOCK_EDITOR_TEMPLATES

    def test_registry_json_objects(self) -> None:
        reg = registry_json_objects()
        assert len(reg) == len(_BLOCK_LIST)
        assert reg[0]["id"]
        new_issue = next(b for b in reg if b["id"] == "new_issue_header")
        assert new_issue["settings_schema"]["emoji"]["label"] == "Префикс в заголовке"


class TestNoPatternOverlap:
    @pytest.mark.parametrize("tpl_name", list(DEFAULT_BLOCK_CONFIGS.keys()))
    def test_no_overlap(self, tpl_name: str) -> None:
        verify_no_overlap(tpl_name)


class TestSanitize:
    def test_normal_text(self) -> None:
        assert _sanitize_setting_value("Новая задача") == "Новая задача"

    def test_single_quote_escaped(self) -> None:
        assert "\\'" in _sanitize_setting_value("it's")

    @pytest.mark.parametrize(
        "bad",
        ["{{ x }}", "{% import os %}", "{# comment #}", "ok {{ x }} ok"],
    )
    def test_jinja_injection_rejected(self, bad: str) -> None:
        with pytest.raises(ValueError, match="Jinja syntax"):
            _sanitize_setting_value(bad)


class TestApplySettings:
    def test_new_issue_header(self) -> None:
        bdef = BLOCK_REGISTRY["new_issue_header"]
        result = apply_block_settings(bdef, {"emoji": "🔔"})
        assert "🔔" in result
        assert "__new_issue_header_emoji__" not in result

    def test_missing_setting_uses_default(self) -> None:
        bdef = BLOCK_REGISTRY["task_change_header"]
        result = apply_block_settings(bdef, {})
        assert "Изменение" in result
        assert "__task_change_header_emoji__" not in result


class TestCompileEtalon:
    @pytest.mark.parametrize("tpl_name", list(DEFAULT_BLOCK_CONFIGS.keys()))
    def test_compile_matches_default_file(self, tpl_name: str) -> None:
        compiled = compile_blocks_to_jinja(DEFAULT_BLOCK_CONFIGS[tpl_name])
        file_html = read_default_file(tpl_name) or ""
        assert _normalize_for_match(compiled) == _normalize_for_match(file_html)

    def test_monolith_templates_compile_byte_identical_to_default_files(self) -> None:
        for name in ("tpl_digest",):
            compiled = compile_blocks_to_jinja(DEFAULT_BLOCK_CONFIGS[name])
            assert compiled == (read_default_file(name) or "")


class TestCompileErrors:
    def test_unknown_block_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown block_id"):
            compile_blocks_to_jinja([BlockConfig("nonexistent", True, 0, {})])

    def test_duplicate_block_raises(self) -> None:
        blocks = [
            BlockConfig("issue_subject", True, 0, {}),
            BlockConfig("issue_subject", True, 1, {}),
        ]
        with pytest.raises(ValueError, match="Duplicate"):
            compile_blocks_to_jinja(blocks)


class TestDecompose:
    def test_decompose_tolerates_pipe_default_without_space(self) -> None:
        blocks = DEFAULT_BLOCK_CONFIGS["tpl_new_issue"]
        j = compile_blocks_to_jinja(blocks).replace("| default", "|default")
        assert jinja_to_blocks(j, "tpl_new_issue") is not None

    def test_decompose_double_quoted_empty_default(self) -> None:
        blocks = DEFAULT_BLOCK_CONFIGS["tpl_new_issue"]
        j = compile_blocks_to_jinja(blocks).replace("default('')", 'default("")')
        assert jinja_to_blocks(j, "tpl_new_issue") is not None

    def test_prepare_body_html_for_decompose_strips_crlf(self) -> None:
        assert "default('')" in prepare_body_html_for_decompose('{{ emoji|default("") }}')

    @pytest.mark.parametrize("tpl_name", list(DEFAULT_BLOCK_CONFIGS.keys()))
    def test_roundtrip(self, tpl_name: str) -> None:
        blocks_orig = DEFAULT_BLOCK_CONFIGS[tpl_name]
        jinja_v1 = compile_blocks_to_jinja(blocks_orig)
        decomposed = jinja_to_blocks(jinja_v1, tpl_name)
        assert decomposed is not None
        jinja_v2 = compile_blocks_to_jinja(decomposed)
        assert _normalize_for_match(jinja_v1) == _normalize_for_match(jinja_v2)

    def test_custom_jinja_returns_none(self) -> None:
        assert jinja_to_blocks("<div>{{ custom }}</div>", "tpl_new_issue") is None

    def test_partial_match_returns_none(self) -> None:
        jinja = compile_blocks_to_jinja([BlockConfig("issue_subject", True, 0, {})]) + "\n<p>Extra</p>"
        assert jinja_to_blocks(jinja, "tpl_new_issue") is None

    def test_empty_returns_none(self) -> None:
        assert jinja_to_blocks("", "tpl_new_issue") is None

    def test_full_body_digest_custom_returns_none(self) -> None:
        base = compile_blocks_to_jinja(DEFAULT_BLOCK_CONFIGS["tpl_digest"])
        assert jinja_to_blocks(base + "\n<p>extra</p>", "tpl_digest") is None


class TestNormalize:
    def test_collapses_whitespace(self) -> None:
        assert _normalize_for_match("a   b\n\tc") == "a b c"

    def test_replaces_double_quotes(self) -> None:
        assert _normalize_for_match('a="b"') == "a='b'"

    def test_strips(self) -> None:
        assert _normalize_for_match("  hello  ") == "hello"
