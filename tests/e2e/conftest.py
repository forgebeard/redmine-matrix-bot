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
import http.cookiejar

from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener

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


@pytest.fixture(scope="session")
def e2e_credentials(e2e_admin_url: str) -> tuple[str, str] | None:
    """
    Возвращает (email, password) для входа: из env или после одноразовой регистрации /setup.
    """
    email = (os.getenv("E2E_ADMIN_EMAIL") or "").strip()
    password = (os.getenv("E2E_ADMIN_PASSWORD") or "").strip()
    if email and password:
        return email, password

    # Попробовать создать первого админа, если БД «пустая» (нужна сессия CSRF-cookie)
    cj = http.cookiejar.CookieJar()
    opener = build_opener(HTTPCookieProcessor(cj))
    try:
        req_get = Request(f"{e2e_admin_url}/setup", method="GET")
        with opener.open(req_get, timeout=10.0) as r:
            if r.status != 200:
                return None
            body = r.read().decode("utf-8", errors="replace")
    except (HTTPError, URLError, OSError):
        return None

    if "Первичная настройка" not in body:
        return None

    token = _csrf_from_html(body)
    if not token:
        return None

    new_email = "e2e_admin@e2e.local"
    new_password = "StrongPassword123E2e!!"
    encoded = urlencode(
        {"email": new_email, "password": new_password, "csrf_token": token}
    ).encode("utf-8")
    post = Request(
        f"{e2e_admin_url}/setup",
        data=encoded,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with opener.open(post, timeout=15.0) as r:
            if r.status not in (200, 303, 302):
                return None
    except HTTPError as e:
        if e.code in (409, 400):
            return None
        raise

    return new_email, new_password
