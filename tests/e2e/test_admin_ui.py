"""
Браузерные смоук-тесты админки (Playwright).

Без E2E_ADMIN_* полный вход выполняется только если в БД ещё нет администратора
(фикстура пытается один раз пройти /setup).
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect


@pytest.mark.e2e
def test_login_page_visible(page: Page, e2e_admin_url: str) -> None:
    page.goto(f"{e2e_admin_url}/login")
    expect(page.get_by_role("heading", name="Вход в панель")).to_be_visible()
    expect(page.get_by_label("Login")).to_be_visible()
    expect(page.get_by_label("Пароль", exact=True)).to_be_visible()


@pytest.mark.e2e
def test_unauthenticated_users_redirects_to_login(page: Page, e2e_admin_url: str) -> None:
    page.goto(f"{e2e_admin_url}/users")
    expect(page).to_have_url(re.compile(r".*/login/?$"))


@pytest.mark.e2e
def test_login_reaches_shell_after_auth(
    page: Page,
    e2e_admin_url: str,
    e2e_credentials: tuple[str, str] | None,
) -> None:
    if e2e_credentials is None:
        pytest.skip(
            "Нет учётных данных: задайте E2E_ADMIN_LOGIN (или E2E_ADMIN_EMAIL) и E2E_ADMIN_PASSWORD "
            "или очистите таблицу админов для одноразового /setup"
        )
    login, password = e2e_credentials
    page.goto(f"{e2e_admin_url}/login")
    page.get_by_label("Login").fill(login)
    page.get_by_label("Пароль", exact=True).fill(password)
    page.get_by_role("button", name="Войти").click()
    # После входа — дашборд или мастер интеграций
    shell = page.get_by_role("heading", name="Обзор").or_(
        page.get_by_role("heading", name="Первичная настройка подключений")
    )
    expect(shell).to_be_visible(timeout=15_000)


@pytest.mark.e2e
def test_groups_page_visible_after_auth(
    page: Page,
    e2e_admin_url: str,
    e2e_credentials: tuple[str, str] | None,
) -> None:
    if e2e_credentials is None:
        pytest.skip("Нет учётных данных для входа")
    login, password = e2e_credentials
    page.goto(f"{e2e_admin_url}/login")
    page.get_by_label("Login").fill(login)
    page.get_by_label("Пароль", exact=True).fill(password)
    page.get_by_role("button", name="Войти").click()
    page.goto(f"{e2e_admin_url}/groups")
    expect(page.get_by_role("heading", name="Группы")).to_be_visible(timeout=10_000)
