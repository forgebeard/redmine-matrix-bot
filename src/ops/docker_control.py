from __future__ import annotations

import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class DockerControlError(RuntimeError):
    pass


def _docker_base_url() -> str:
    host = (os.getenv("DOCKER_HOST") or "").strip()
    if host.startswith("tcp://"):
        return "http://" + host.removeprefix("tcp://")
    if host.startswith("http://") or host.startswith("https://"):
        return host
    raise DockerControlError("DOCKER_HOST не настроен для runtime-control")


def _service_name() -> str:
    name = (os.getenv("DOCKER_TARGET_SERVICE") or "").strip()
    return name or "bot"


def _project_name() -> str | None:
    v = (os.getenv("COMPOSE_PROJECT_NAME") or os.getenv("DOCKER_COMPOSE_PROJECT") or "").strip()
    return v if v else None


def _docker_timeout_seconds(method: str, path: str) -> float:
    """
    HTTP-таймаут к Docker API.

    POST .../stop ждёт фактической остановки контейнера (часто до десятков секунд); короткий timeout
    даёт ложную ошибку «timed out», хотя stop в демоне ещё идёт.
    """
    raw = (os.getenv("DOCKER_CONTROL_TIMEOUT") or "").strip()
    try:
        user_cap = float(raw) if raw else 0.0
    except ValueError:
        user_cap = 0.0
    if user_cap > 0:
        return max(1.0, min(user_cap, 300.0))
    # Дефолт без env: запас под stop (SIGTERM→SIGKILL у Docker по умолчанию до ~10 с + прокси/нагрузка)
    if method == "POST" and path.rstrip("/").endswith("/stop"):
        return 90.0
    return 30.0


def _docker_request(method: str, path: str) -> tuple[int, Any]:
    base = _docker_base_url().rstrip("/")
    # Пустое тело + явный Content-Length: часть прокси/urllib некорректно обрабатывает POST без data
    data: bytes | None = b"" if method == "POST" else None
    req = Request(f"{base}{path}", data=data, method=method)
    timeout = _docker_timeout_seconds(method, path)
    try:
        with urlopen(req, timeout=timeout) as r:
            payload = r.read().decode("utf-8", errors="replace")
            if not payload:
                return r.status, None
            try:
                return r.status, json.loads(payload)
            except json.JSONDecodeError:
                return r.status, payload
    except HTTPError as e:
        # Docker Engine: POST .../stop → 304 если контейнер уже остановлен; POST .../start → 304 если уже запущен.
        if method == "POST" and e.code == 304:
            ep = path.rstrip("/")
            if ep.endswith("/stop") or ep.endswith("/start"):
                return 304, None
        text = e.read().decode("utf-8", errors="replace")
        raise DockerControlError(f"Docker API HTTP {e.code}: {text}") from e
    except URLError as e:
        msg = str(e).lower()
        hint = ""
        if "timed out" in msg or "timeout" in msg:
            hint = (
                f" (таймаут HTTP {timeout:g} c; для stop Docker ждёт остановки контейнера — увеличьте "
                "DOCKER_CONTROL_TIMEOUT в .env, например 120)"
            )
        raise DockerControlError(f"Docker API недоступен: {e}{hint}") from e


def _containers_with_labels(service: str, compose_project: str | None) -> list[dict[str, Any]]:
    labels = [f"com.docker.compose.service={service}"]
    if compose_project:
        labels.append(f"com.docker.compose.project={compose_project}")
    filters = {"label": labels}
    query = urlencode({"all": "1", "filters": json.dumps(filters)})
    _, payload = _docker_request("GET", f"/containers/json?{query}")
    return payload if isinstance(payload, list) else []


def _containers_all() -> list[dict[str, Any]]:
    _, payload = _docker_request("GET", "/containers/json?all=1")
    return payload if isinstance(payload, list) else []


def _row_running(row: dict[str, Any]) -> bool:
    return str(row.get("State") or "").lower() == "running"


def _find_target_container_id_from_list(service: str, rows: list[dict[str, Any]]) -> str | None:
    """
    Fallback: полный список контейнеров (иногда фильтр label на API ведёт себя иначе) или нет меток compose.

    Порядок совпадений: метка com.docker.compose.service; подстрока в имени (DOCKER_TARGET_CONTAINER_SUBSTRING);
    имя в стиле Compose v2: *-{service}-* (например project-bot-1).
    """
    sub = (os.getenv("DOCKER_TARGET_CONTAINER_SUBSTRING") or "").strip()
    token = f"-{service}-"
    matches: list[dict[str, Any]] = []
    for row in rows:
        cid = row.get("Id")
        if not cid:
            continue
        labels = row.get("Labels") or {}
        names = row.get("Names") or []
        name_blob = " ".join(str(n) for n in names)
        if labels.get("com.docker.compose.service") == service:
            matches.append(row)
            continue
        if sub and sub in name_blob:
            matches.append(row)
            continue
        if token in name_blob:
            matches.append(row)
    if not matches:
        return None
    for row in matches:
        if _row_running(row):
            return row.get("Id")
    return matches[0].get("Id")


def _find_target_container_id() -> str | None:
    """
    Ищем контейнер по меткам compose.

    Если задан COMPOSE_PROJECT_NAME и он не совпадает с реальной меткой проекта у контейнеров,
    список будет пустой — тогда повторяем поиск только по имени сервиса (типичный сбой UI Stop/Start).
    Если и это пусто — один запрос со списком всех контейнеров и эвристики по меткам/имени.
    """
    service = _service_name()
    project = _project_name()
    if project:
        rows = _containers_with_labels(service, project)
        if rows:
            return rows[0].get("Id")
    rows = _containers_with_labels(service, None)
    if rows:
        return rows[0].get("Id")
    return _find_target_container_id_from_list(service, _containers_all())


def get_service_status() -> dict[str, Any]:
    cid = _find_target_container_id()
    if not cid:
        return {"service": _service_name(), "state": "not_found", "container_name": ""}
    _, payload = _docker_request("GET", f"/containers/{cid}/json")
    state = "unknown"
    running = False
    name = ""
    if isinstance(payload, dict):
        st = payload.get("State") or {}
        running = bool(st.get("Running"))
        state = "running" if running else "stopped"
        name = str(payload.get("Name") or "").lstrip("/")
    return {
        "service": _service_name(),
        "state": state,
        "running": running,
        "container_id": cid,
        "container_name": name,
    }


def control_service(action: str) -> dict[str, Any]:
    allowed = {"start", "stop", "restart"}
    if action not in allowed:
        raise DockerControlError("Недопустимая операция")
    cid = _find_target_container_id()
    if not cid:
        hint = (
            f"не найден контейнер с com.docker.compose.service={_service_name()}"
            + (f" и project={_project_name()!r}" if _project_name() else "")
            + ". Проверьте DOCKER_TARGET_SERVICE; при необходимости уберите или исправьте COMPOSE_PROJECT_NAME в .env"
            + " Либо задайте DOCKER_TARGET_CONTAINER_SUBSTRING (уникальная подстрока в docker ps --format '{{{{.Names}}}}')."
        )
        raise DockerControlError(hint)
    http_status, _ = _docker_request("POST", f"/containers/{cid}/{action}")
    return {
        "service": _service_name(),
        "action": action,
        "container_id": cid,
        "docker_http_status": http_status,
    }
