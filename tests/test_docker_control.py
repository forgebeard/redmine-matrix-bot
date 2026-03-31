import json

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


def test_get_service_status_running(monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "tcp://proxy:2375")
    monkeypatch.setenv("DOCKER_TARGET_SERVICE", "bot")

    def fake_open(req, timeout=0):  # noqa: ARG001
        url = req.full_url
        if "/containers/json?" in url:
            return _Resp(200, json.dumps([{"Id": "abc123"}]))
        if "/containers/abc123/json" in url:
            return _Resp(200, json.dumps({"State": {"Running": True}}))
        raise AssertionError(url)

    monkeypatch.setattr(docker_control, "urlopen", fake_open)
    st = docker_control.get_service_status()
    assert st["state"] == "running"
    assert st["container_id"] == "abc123"
