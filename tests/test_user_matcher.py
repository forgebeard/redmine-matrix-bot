"""
Тесты src/user_matcher.py — транслитерация, скоринг, парсинг URL, извлечение имени.

Не требуют HTTP, работают на чистой логике.
"""

from __future__ import annotations

import pytest

from user_matcher import (
    Match,
    extract_matrix_localpart,
    extract_name,
    find_best_match,
    generate_translit_queries,
    is_human_entry,
    normalize_yo,
    parse_url_to_endpoint,
    score_matrix_candidate,
    transliterate,
    transliterate_variants,
)


# ═══════════════════════════════════════════════════════════════════════════
# Транслитерация
# ═══════════════════════════════════════════════════════════════════════════


class TestTransliterate:
    def test_simple_name(self):
        assert transliterate("Денис") == "denis"

    def test_surname(self):
        # ч → ch, ё → yo → fomicheov
        result = transliterate("Фомичёв")
        assert result in ("fomichyov", "fomicheov")

    def test_full_name(self):
        result = transliterate("Денис Фомичёв")
        assert "denis" in result and ("fomichyov" in result or "fomicheov" in result)

    def test_yo_replacement(self):
        assert normalize_yo("Фомичёв") == "Фомичев"
        assert normalize_yo("ёжик") == "ежик"

    def test_variants_include_yo_forms(self):
        variants = transliterate_variants("Фомичёв")
        # Должны быть варианты и с ё→yo, и с ё→e
        assert len(variants) >= 2
        assert any("fomichev" in v for v in variants)


class TestGenerateTranslitQueries:
    def test_two_parts(self):
        queries = generate_translit_queries("Денис Фомичёв")
        # Должны быть варианты с denis + fomichev/fomichyov
        has_name = any("denis" in q for q in queries)
        has_surname = any("fomichev" in q or "fomichyov" in q for q in queries)
        assert has_name and has_surname

    def test_single_part_returns_empty(self):
        queries = generate_translit_queries("")
        assert queries == []

    def test_initials_format(self):
        queries = generate_translit_queries("Денис Фомичёв")
        # Инициал + фамилия: d.fomicheov, d_fomicheov
        has_initial = any(q.startswith("d.") or q.startswith("d_") for q in queries)
        assert has_initial


# ═══════════════════════════════════════════════════════════════════════════
# Парсинг URL Redmine
# ═══════════════════════════════════════════════════════════════════════════


class TestParseUrlToEndpoint:
    def test_group_url(self):
        base, api, params, etype = parse_url_to_endpoint("https://redmine.example.com/groups/5")
        assert base == "https://redmine.example.com"
        assert api == "https://redmine.example.com/groups/5.json"
        assert etype == "group"
        assert params == {"include": "users"}

    def test_project_members_url(self):
        _, api, _, etype = parse_url_to_endpoint(
            "https://redmine.example.com/projects/myproj/members"
        )
        assert "memberships.json" in api
        assert etype == "memberships"

    def test_users_list_url(self):
        _, api, _, etype = parse_url_to_endpoint("https://redmine.example.com/users")
        assert api.endswith("/users.json")
        assert etype == "users"

    def test_single_user_url(self):
        _, api, _, etype = parse_url_to_endpoint("https://redmine.example.com/users/42")
        assert api.endswith("/users/42.json")
        assert etype == "single_user"

    def test_unrecognized_url(self):
        base, api, params, etype = parse_url_to_endpoint("https://redmine.example.com/dashboard")
        assert api is None
        assert etype is None


# ═══════════════════════════════════════════════════════════════════════════
# Извлечение имени
# ═══════════════════════════════════════════════════════════════════════════


class TestExtractName:
    def test_name_field(self):
        assert extract_name({"name": "Иванов Иван", "id": 1}) == "Иванов Иван"

    def test_firstname_lastname(self):
        assert extract_name({"firstname": "Иван", "lastname": "Иванов", "id": 2}) == "Иванов Иван"

    def test_login_fallback(self):
        assert extract_name({"login": "iivanov", "id": 3}) == "iivanov"

    def test_nested_user(self):
        assert extract_name({"user": {"name": "Петров Пётр"}, "id": 4}) == "Петров Пётр"

    def test_empty(self):
        assert extract_name({}) == ""


class TestIsHumanEntry:
    def test_valid_two_words(self):
        assert is_human_entry({"name": "Иванов Иван", "id": 1}) is True

    def test_valid_three_words(self):
        assert is_human_entry({"name": "Иванов Иван Сергеевич", "id": 2}) is True

    def test_single_word_rejected(self):
        assert is_human_entry({"name": "Иванов", "id": 3}) is False

    def test_system_word_rejected(self):
        assert is_human_entry({"name": "Техподдержка Бот", "id": 4}) is False

    def test_empty_name_rejected(self):
        assert is_human_entry({"name": "", "id": 5}) is False

    def test_non_user_type_rejected(self):
        assert is_human_entry({"name": "Иванов Иван", "type": "Group", "id": 6}) is False


# ═══════════════════════════════════════════════════════════════════════════
# Скоринг Matrix кандидатов
# ═══════════════════════════════════════════════════════════════════════════


class TestScoreMatrixCandidate:
    def test_exact_cyrillic_match(self):
        """Идеальное совпадение по кириллице в display_name."""
        score = score_matrix_candidate(
            "Денис Фомичёв",
            {"display_name": "Фомичёв Денис", "user_id": "@denis_fomichev:server"},
        )
        assert score > 0

    def test_translit_match(self):
        """Совпадение по транслиту в localpart."""
        score = score_matrix_candidate(
            "Денис Фомичёв",
            {"display_name": "", "user_id": "@denis_fomichev:server"},
        )
        # Должно найти denis + fomicheov/fomichev в localpart
        assert score > 0

    def test_only_first_name_no_match(self):
        """Только имя «Денис» → denis_fomichev НЕ должен пройти."""
        score = score_matrix_candidate(
            "Денис",
            {"display_name": "", "user_id": "@denis_fomichev:server"},
        )
        assert score == 0

    def test_unrelated_user(self):
        """Совершенно другой человек — score = 0."""
        score = score_matrix_candidate(
            "Иванов Иван",
            {"display_name": "Петров Пётр", "user_id": "@ppetrov:server"},
        )
        assert score == 0

    def test_partial_match_lower_than_exact(self):
        """Однофамилец должен получить меньший score чем полное совпадение."""
        partial = score_matrix_candidate(
            "Денис Фомичёв",
            {"display_name": "Алексей Фомичёв", "user_id": "@afomichev:server"},
        )
        exact = score_matrix_candidate(
            "Денис Фомичёв",
            {"display_name": "Денис Фомичёв", "user_id": "@denis_fomichev:server"},
        )
        # Частичное совпадение должно быть строго меньше полного
        assert partial < exact


class TestFindBestMatch:
    def test_returns_best_score(self):
        users = [
            {"display_name": "Денис Фомичёв", "user_id": "@denis:server"},
            {"display_name": "Денис Фомичёв Инженер", "user_id": "@denis_f:server"},
        ]
        best = find_best_match("Денис Фомичёв", users)
        assert best is not None
        assert best["user_id"] == "@denis:server"

    def test_no_match_below_threshold(self):
        best = find_best_match("Денис", [{"display_name": "", "user_id": "@denis_fomichev:server"}])
        assert best is None

    def test_empty_results(self):
        assert find_best_match("Денис", []) is None


# ═══════════════════════════════════════════════════════════════════════════
# Extract Matrix localpart
# ═══════════════════════════════════════════════════════════════════════════


class TestExtractMatrixLocalpart:
    def test_valid_user_id(self):
        assert extract_matrix_localpart({"user_id": "@denis_fomichev:server"}) == "denis_fomichev"

    def test_none_user(self):
        assert extract_matrix_localpart(None) is None

    def test_empty_user(self):
        assert extract_matrix_localpart({}) is None


# ═══════════════════════════════════════════════════════════════════════════
# Match dataclass
# ═══════════════════════════════════════════════════════════════════════════


class TestMatch:
    def test_is_found(self):
        m = Match(redmine_name="Иванов Иван", redmine_id=1, status="found")
        assert m.is_found is True
        assert m.is_existing is False

    def test_is_existing(self):
        m = Match(redmine_name="Иванов Иван", redmine_id=1, status="existing")
        assert m.is_existing is True
        assert m.is_found is False

    def test_not_found(self):
        m = Match(redmine_name="Иванов Иван", redmine_id=1, status="not_found")
        assert m.is_found is False
        assert m.is_existing is False
