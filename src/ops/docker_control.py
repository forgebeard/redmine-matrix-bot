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


def _docker_request(method: str, path: str) -> tuple[int, Any]:
    base = _docker_base_url().rstrip("/")
    req = Request(f"{base}{path}", method=method)
    try:
        with urlopen(req, timeout=5.0) as r:
            payload = r.read().decode("utf-8", errors="replace")
            if not payload:
                return r.status, None
            try:
                return r.status, json.loads(payload)
            except json.JSONDecodeError:
                return r.status, payload
    except HTTPError as e:
        text = e.read().decode("utf-8", errors="replace")
        raise DockerControlError(f"Docker API HTTP {e.code}: {text}") from e
    except URLError as e:
        raise DockerControlError(f"Docker API недоступен: {e}") from e


def _find_target_container_id() -> str | None:
    service = _service_name()
    filters: dict[str, list[str]] = {
        "label": [f"com.docker.compose.service={service}"],
    }
    project = _project_name()
    if project:
        filters["label"].append(f"com.docker.compose.project={project}")
    query = urlencode({"all": "1", "filters": json.dumps(filters)})
    _, payload = _docker_request("GET", f"/containers/json?{query}")
    if not isinstance(payload, list) or not payload:
        return None
    return payload[0].get("Id")


def get_service_status() -> dict[str, Any]:
    cid = _find_target_container_id()
    if not cid:
        return {"service": _service_name(), "state": "not_found"}
    _, payload = _docker_request("GET", f"/containers/{cid}/json")
    state = "unknown"
    running = False
    if isinstance(payload, dict):
        st = payload.get("State") or {}
        running = bool(st.get("Running"))
        state = "running" if running else "stopped"
    return {"service": _service_name(), "state": state, "running": running, "container_id": cid}


def control_service(action: str) -> dict[str, Any]:
    allowed = {"start", "stop", "restart"}
    if action not in allowed:
        raise DockerControlError("Недопустимая операция")
    cid = _find_target_container_id()
    if not cid:
        raise DockerControlError("Контейнер сервиса не найден")
    _docker_request("POST", f"/containers/{cid}/{action}")
    return {"service": _service_name(), "action": action, "container_id": cid}
