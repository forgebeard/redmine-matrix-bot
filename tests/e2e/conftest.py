"""
E2E: реальный HTTP-сервер (uvicorn) + браузер Playwright.

Запуск без БД: тесты пропускаются (нет DATABASE_URL на Postgres).
Установка: pip install -r requirements-test.txt && playwright install chromium

Пример полного сценария с входом:
  E2E_ADMIN_EMAIL=... E2E_ADMIN_PASSWORD=... pytest tests/e2e/ -q
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Generator

from urllib.error import HTTPError, URLError
from urllib.request import Request

import httpx
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_http_ok(url: str, timeout_s: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_s
    last_err: Exception | None = None
    opener = build_opener()
    while time.monotonic() < deadline:
        try:
            req = Request(url, method="GET")
            with opener.open(req, timeout=2.0) as r:
                if r.status == 200:
                    return
        except (HTTPError, URLError, OSError, TimeoutError) as e:
            last_err = e
            time.sleep(0.15)
    raise RuntimeError(f"Сервер не ответил 200 на {url}: {last_err}")


@pytest.fixture(scope="session")
def e2e_admin_url() -> Generator[str, None, None]:
    if not os.getenv("DATABASE_URL", "").startswith("postgresql://"):
        pytest.skip("E2E требует Postgres (DATABASE_URL)")

    pytest.importorskip("playwright")

    port = _free_port()
    env = os.environ.copy()
    env.setdefault("APP_MASTER_KEY", "0123456789abcdef0123456789abcdef")
    env.setdefault("SMTP_MOCK", "1")
    env.setdefault("PYTHONPATH", str(PROJECT_ROOT))

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "admin_main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        _wait_http_ok(f"{base}/health")
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture
def page(e2e_admin_url: str):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            base_url=e2e_admin_url,
            locale="ru-RU",
        )
        pg = context.new_page()
        try:
            yield pg
        finally:
            context.close()
            browser.close()


def _csrf_from_html(html: str) -> str | None:
    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', html)
    if m:
        return m.group(1)
    m = re.search(r'value="([^"]+)"\s+name="csrf_token"', html)
    return m.group(1) if m else None


def _bootstrap_e2e_admin_via_http(base: str) -> tuple[str, str] | None:
    """Вход admin/admin → смена учётки (httpx, без следования редиректам POST)."""
    new_login = "e2e_admin"
    new_password = "StrongPassword123E2e!!"
    try:
        with httpx.Client(base_url=base, follow_redirects=False, timeout=20.0) as cx:
            r0 = cx.get("/login")
            if r0.status_code != 200:
                return None
            tok = _csrf_from_html(r0.text)
            if not tok:
                return None
            r1 = cx.post(
                "/login",
                data={"login": "admin", "password": "admin", "csrf_token": tok},
            )
            if r1.status_code not in (302, 303):
                return None
            r2 = cx.get("/me/bootstrap-credentials")
            if r2.status_code != 200 or "Смена учётных данных" not in r2.text:
                return None
            tok2 = _csrf_from_html(r2.text)
            if not tok2:
                return None
            r3 = cx.post(
                "/me/bootstrap-credentials",
                data={
                    "login": new_login,
                    "password": new_password,
                    "password_confirm": new_password,
                    "csrf_token": tok2,
                },
            )
            if r3.status_code not in (302, 303, 200):
                return None
    except (httpx.HTTPError, OSError):
        return None
    return new_login, new_password


@pytest.fixture(scope="session")
def e2e_credentials(e2e_admin_url: str) -> tuple[str, str] | None:
    """
    Возвращает (login, password): из env или после входа admin/admin и смены учётки на e2e_*.
    """
    login = (os.getenv("E2E_ADMIN_LOGIN") or os.getenv("E2E_ADMIN_EMAIL") or "").strip()
    password = (os.getenv("E2E_ADMIN_PASSWORD") or "").strip()
    if login and password:
        return login, password

    return _bootstrap_e2e_admin_via_http(e2e_admin_url)
