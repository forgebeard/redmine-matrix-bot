import json
from io import BytesIO
from urllib.error import HTTPError

import pytest

from ops import docker_control


class _Resp:
    def __init__(self, status: int, payload: str):
        self.status = status
        self._payload = payload.encode("utf-8")

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_control_service_rejects_invalid_action():
    with pytest.raises(docker_control.DockerControlError):
        docker_control.control_service("rm")


def test_docker_timeout_stop_longer_than_list(monkeypatch):
    monkeypatch.delenv("DOCKER_CONTROL_TIMEOUT", raising=False)
    assert docker_control._docker_timeout_seconds("GET", "/containers/json?all=1") == 30.0
    assert docker_control._docker_timeout_seconds("POST", "/containers/abc/stop") == 90.0
    assert docker_control._docker_timeout_seconds("POST", "/containers/abc/stop/") == 90.0


def test_docker_timeout_env_override(monkeypatch):
    monkeypatch.setenv("DOCKER_CONTROL_TIMEOUT", "45")
    assert docker_control._docker_timeout_seconds("POST", "/containers/x/stop") == 45.0
    assert docker_control._docker_timeout_seconds("GET", "/x") == 45.0


def test_stop_accepts_http_304_already_stopped(monkeypatch):
    """Docker возвращает 304, если контейнер уже в нужном состоянии — не считаем ошибкой."""

    monkeypatch.setenv("DOCKER_HOST", "tcp://proxy:2375")

    def fake_open(req, timeout=0):  # noqa: ARG001
        u = req.full_url
        if "/containers/json?" in u:
            return _Resp(200, json.dumps([{"Id": "abc123"}]))
        if "/containers/abc123/stop" in u:
            raise HTTPError(u, 304, "Not Modified", hdrs=None, fp=BytesIO(b""))
        raise AssertionError(u)

    monkeypatch.setattr(docker_control, "urlopen", fake_open)
    out = docker_control.control_service("stop")
    assert out["action"] == "stop"
    assert out["container_id"] == "abc123"
    assert out["docker_http_status"] == 304


def test_get_service_status_running(monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "tcp://proxy:2375")
    monkeypatch.setenv("DOCKER_TARGET_SERVICE", "bot")

    def fake_open(req, timeout=0):  # noqa: ARG001
        url = req.full_url
        if "/containers/json?" in url:
            return _Resp(200, json.dumps([{"Id": "abc123"}]))
        if "/containers/abc123/json" in url:
            return _Resp(200, json.dumps({"State": {"Running": True}, "Name": "/proj-bot-1"}))
        raise AssertionError(url)

    monkeypatch.setattr(docker_control, "urlopen", fake_open)
    st = docker_control.get_service_status()
    assert st["state"] == "running"
    assert st["container_id"] == "abc123"
    assert st["container_name"] == "proj-bot-1"


def test_find_container_falls_back_when_compose_project_wrong(monkeypatch):
    """Неверный COMPOSE_PROJECT_NAME: сначала пустой список, затем поиск только по сервису."""
    monkeypatch.setenv("DOCKER_HOST", "tcp://proxy:2375")
    monkeypatch.setenv("DOCKER_TARGET_SERVICE", "bot")
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "wrong_project")

    def fake_open(req, timeout=0):  # noqa: ARG001
        url = req.full_url
        if "/containers/json?" in url:
            if "wrong_project" in url:
                return _Resp(200, json.dumps([]))
            return _Resp(200, json.dumps([{"Id": "fallback-id"}]))
        if "/containers/fallback-id/stop" in url:
            return _Resp(204, "")
        raise AssertionError(url)

    monkeypatch.setattr(docker_control, "urlopen", fake_open)
    out = docker_control.control_service("stop")
    assert out["container_id"] == "fallback-id"
    assert out["action"] == "stop"
    assert out["docker_http_status"] == 204


def test_control_service_not_found_message(monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "tcp://proxy:2375")
    monkeypatch.setenv("DOCKER_TARGET_SERVICE", "bot")
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "p")

    def fake_open(req, timeout=0):  # noqa: ARG001
        if "/containers/json?" in req.full_url:
            return _Resp(200, json.dumps([]))
        raise AssertionError(req.full_url)

    monkeypatch.setattr(docker_control, "urlopen", fake_open)
    with pytest.raises(docker_control.DockerControlError) as ei:
        docker_control.control_service("stop")
    assert "DOCKER_TARGET_SERVICE" in str(ei.value)
    assert "COMPOSE_PROJECT_NAME" in str(ei.value)
    assert "DOCKER_TARGET_CONTAINER_SUBSTRING" in str(ei.value)


def test_find_container_scans_all_when_label_filter_returns_empty(monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "tcp://proxy:2375")
    monkeypatch.setenv("DOCKER_TARGET_SERVICE", "bot")
    monkeypatch.delenv("COMPOSE_PROJECT_NAME", raising=False)

    def fake_open(req, timeout=0):  # noqa: ARG001
        url = req.full_url
        if "/containers/json?" in url:
            if "filters" in url:
                return _Resp(200, json.dumps([]))
            return _Resp(
                200,
                json.dumps(
                    [
                        {
                            "Id": "scan1",
                            "Names": ["/x-bot-1"],
                            "Labels": {"com.docker.compose.service": "bot"},
                            "State": "running",
                        }
                    ]
                ),
            )
        if "/containers/scan1/stop" in url:
            return _Resp(204, "")
        raise AssertionError(url)

    monkeypatch.setattr(docker_control, "urlopen", fake_open)
    out = docker_control.control_service("stop")
    assert out["container_id"] == "scan1"


def test_find_container_name_token_without_compose_labels(monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "tcp://proxy:2375")
    monkeypatch.setenv("DOCKER_TARGET_SERVICE", "bot")
    monkeypatch.delenv("COMPOSE_PROJECT_NAME", raising=False)

    def fake_open(req, timeout=0):  # noqa: ARG001
        url = req.full_url
        if "/containers/json?" in url:
            if "filters" in url:
                return _Resp(200, json.dumps([]))
            return _Resp(
                200,
                json.dumps(
                    [
                        {
                            "Id": "n2",
                            "Names": ["/myproj-bot-1"],
                            "Labels": {},
                            "State": "running",
                        }
                    ]
                ),
            )
        if "/containers/n2/stop" in url:
            return _Resp(204, "")
        raise AssertionError(url)

    monkeypatch.setattr(docker_control, "urlopen", fake_open)
    out = docker_control.control_service("stop")
    assert out["container_id"] == "n2"
