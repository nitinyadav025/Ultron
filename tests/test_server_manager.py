"""Offline tests for Phase 2 llama-server auto-start (no real server, no GPU, no model)."""

from __future__ import annotations

import time
from typing import Any, Callable

import httpx
import pytest

from assistant.brain.server_manager import (
    SERVER_FLAGS,
    LlamaServerError,
    LlamaServerManager,
    health_url_for,
    split_base_url,
)


class FakeProc:
    """Stand-in for subprocess.Popen."""

    def __init__(self, code: int | None = None) -> None:
        self.pid = 4242
        self.code = code
        self.terminated = 0
        self.killed = 0

    def poll(self) -> int | None:
        return self.code

    def terminate(self) -> None:
        self.terminated += 1
        self.code = 0

    def wait(self, timeout: float | None = None) -> int:
        return self.code if self.code is not None else 0

    def kill(self) -> None:
        self.killed += 1
        self.code = -9


def _must_not_spawn(*_args: Any, **_kwargs: Any) -> None:
    raise AssertionError("llama-server must not be started here")


def _manager(
    tmp_path: Any,
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    spawn: Callable[..., Any] | None = None,
    port_open: Callable[[str, int], bool] | None = None,
    timeout: float = 5.0,
) -> LlamaServerManager:
    from assistant.config import load_config

    cfg = load_config()
    cfg.backend = "local"
    cfg.llama_server_binary = str(tmp_path / "llama-server")
    cfg.llama_model_path = str(tmp_path / "model.gguf")
    cfg.llama_server_log = tmp_path / "logs" / "llama-server.log"
    cfg.llama_start_timeout_sec = timeout
    (tmp_path / "llama-server").write_text("#!/bin/sh\n", encoding="utf-8")
    (tmp_path / "model.gguf").write_text("", encoding="utf-8")
    return LlamaServerManager(
        cfg,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        spawn=spawn or _must_not_spawn,
        port_open=port_open or (lambda _h, _p: False),
        sleep=lambda _s: time.sleep(0.005),
    )


def _ready(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"status": "ok"})


def _loading(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(503, json={"error": {"message": "Loading model"}})


def test_url_helpers() -> None:
    assert split_base_url("http://127.0.0.1:9931/v1") == ("http", "127.0.0.1", 9931)
    assert health_url_for("http://127.0.0.1:9931/v1") == "http://127.0.0.1:9931/health"
    assert health_url_for("127.0.0.1:9931/v1") == "http://127.0.0.1:9931/health"


def test_reuses_already_running_server(tmp_path: Any) -> None:
    manager = _manager(tmp_path, _ready)
    messages: list[str] = []
    assert manager.ensure_running(progress=messages.append) is False
    assert any("reusing" in m for m in messages)
    manager.stop()  # nothing of ours to stop
    assert True


def test_waits_for_a_server_that_is_already_loading(tmp_path: Any) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return _ready(request) if calls["n"] > 2 else _loading(request)

    manager = _manager(tmp_path, handler)
    assert manager.ensure_running() is False
    assert calls["n"] >= 3


def test_starts_server_with_exact_flags(tmp_path: Any) -> None:
    state = {"ready": False}
    spawned: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if state["ready"]:
            return _ready(request)
        raise httpx.ConnectError("connection refused", request=request)

    def spawn(cmd: list[str], **kwargs: Any) -> FakeProc:
        spawned.append({"cmd": cmd, "kwargs": kwargs})
        state["ready"] = True
        return FakeProc()

    manager = _manager(tmp_path, handler, spawn=spawn)
    assert manager.ensure_running() is True

    assert len(spawned) == 1
    cmd = spawned[0]["cmd"]
    expected_flags = list(SERVER_FLAGS)
    assert cmd[1] == "-m" and cmd[2].endswith("model.gguf")
    assert cmd[3 : 3 + len(expected_flags)] == expected_flags
    assert cmd[cmd.index("--host") + 1] == "127.0.0.1"
    assert cmd[cmd.index("--port") + 1] == "9931"
    assert cmd[cmd.index("--reasoning") + 1] == "off"

    # Child output goes to logs/llama-server.log, not to the terminal.
    assert (tmp_path / "logs" / "llama-server.log").exists()

    proc = manager._proc
    manager.stop()
    assert proc.terminated == 1
    manager.stop()  # idempotent
    assert proc.terminated == 1


def test_occupied_port_is_a_clear_error(tmp_path: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    manager = _manager(tmp_path, handler, port_open=lambda _h, _p: True)
    with pytest.raises(LlamaServerError, match="another program"):
        manager.ensure_running()


def test_unexpected_http_status_is_a_clear_error(tmp_path: Any) -> None:
    manager = _manager(tmp_path, lambda _r: httpx.Response(404, text="not found"))
    with pytest.raises(LlamaServerError, match="used by another program"):
        manager.ensure_running()


def test_timeout_stops_the_child(tmp_path: Any) -> None:
    state = {"spawned": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if not state["spawned"]:
            raise httpx.ConnectError("connection refused", request=request)
        return _loading(request)

    def spawn(*_args: Any, **_kwargs: Any) -> FakeProc:
        state["spawned"] = True
        return FakeProc()

    manager = _manager(tmp_path, handler, spawn=spawn, timeout=1.0)
    started = time.monotonic()
    with pytest.raises(LlamaServerError, match="did not become ready"):
        manager.ensure_running()
    assert time.monotonic() - started < 10
    assert state["spawned"] is True
    assert manager._proc is None  # the child was released after the timeout


def test_child_that_dies_during_startup_reports_the_log(tmp_path: Any) -> None:
    state = {"spawned": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if not state["spawned"]:
            raise httpx.ConnectError("connection refused", request=request)
        return _loading(request)

    def spawn(*_args: Any, **_kwargs: Any) -> FakeProc:
        state["spawned"] = True
        return FakeProc(code=1)

    manager = _manager(tmp_path, handler, spawn=spawn)
    with pytest.raises(LlamaServerError, match="exited during startup"):
        manager.ensure_running()


def test_never_starts_a_second_copy_in_one_run(tmp_path: Any) -> None:
    state = {"ready": False}
    spawned: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if state["ready"]:
            return _ready(request)
        raise httpx.ConnectError("connection refused", request=request)

    def spawn(cmd: list[str], **_kwargs: Any) -> FakeProc:
        spawned.append(cmd)
        state["ready"] = True
        return FakeProc()

    manager = _manager(tmp_path, handler, spawn=spawn)
    assert manager.ensure_running() is True
    assert manager.ensure_running() is False
    assert len(spawned) == 1


def test_missing_binary_is_a_clear_error(tmp_path: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    manager = _manager(tmp_path, handler)
    (tmp_path / "llama-server").unlink()
    with pytest.raises(LlamaServerError, match="binary not found"):
        manager.ensure_running()
