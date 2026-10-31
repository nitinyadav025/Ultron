"""Auto-start and lifecycle management for the local llama.cpp server (Phase 2).

On startup Ultron asks http://127.0.0.1:9931/health whether the brain is up.

* ready        -> reuse it (even one started by hand) and never stop it.
* loading      -> a llama-server is already booting; just wait for it.
* free port    -> launch llama-server with the fixed flags, log to logs/llama-server.log,
                  wait up to 90 s, and terminate the child again when Ultron exits.
* occupied     -> some other program owns the port; fail with a clear message instead
                  of starting a second copy.
"""

from __future__ import annotations

import atexit
import logging
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import httpx

from assistant.config import AssistantConfig

logger = logging.getLogger("ultron.server")

# Fixed server flags from the handoff (--reasoning off keeps replies at ~0.5 s).
SERVER_FLAGS: tuple[str, ...] = (
    "-ngl",
    "99",
    "--jinja",
    "-c",
    "8192",
    "--temp",
    "0.1",
    "--top-k",
    "50",
    "--repeat-penalty",
    "1.1",
    "--reasoning",
    "off",
)

ProgressCallback = Callable[[str], None]


class LlamaServerError(RuntimeError):
    """Raised when the local LLM server is missing, misconfigured, or on the wrong port."""


def split_base_url(base_url: str) -> tuple[str, str, int]:
    """Return (scheme, host, port) for an OpenAI-style base URL such as http://127.0.0.1:9931/v1."""
    parsed = urlparse(base_url if "://" in base_url else f"http://{base_url}")
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return parsed.scheme or "http", host, port


def health_url_for(base_url: str) -> str:
    """Build the llama.cpp /health URL from the configured base URL."""
    scheme, host, port = split_base_url(base_url)
    return f"{scheme}://{host}:{port}/health"


def port_is_open(host: str, port: int, timeout: float = 0.5) -> bool:
    """Return True if something is listening on host:port."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _probe(
    http: httpx.Client,
    health_url: str,
    host: str,
    port: int,
    port_open: Callable[[str, int], bool],
) -> tuple[str, str]:
    """Classify the endpoint as ready, loading, free, or occupied."""
    try:
        resp = http.get(health_url)
    except httpx.ConnectError as exc:
        if port_open(host, port):
            return "occupied", f"port {port} is open but /health refused the request ({exc})"
        return "free", f"nothing listening on {host}:{port}"
    except httpx.HTTPError as exc:  # read timeout etc. — something is listening
        return "loading", f"health did not answer cleanly ({type(exc).__name__})"

    if resp.status_code == 200:
        return "ready", "health 200"
    if resp.status_code == 503:
        return "loading", "health 503 (model loading)"
    return "occupied", f"/health returned HTTP {resp.status_code}"


def server_status(config: AssistantConfig) -> tuple[str, str]:
    """Read-only probe used by `main.py doctor` (never starts anything)."""
    _, host, port = split_base_url(config.llama_base_url)
    with httpx.Client(timeout=httpx.Timeout(2.0), trust_env=False) as http:
        return _probe(http, health_url_for(config.llama_base_url), host, port, port_is_open)


class LlamaServerManager:
    """Own the llama-server child process for the lifetime of one Ultron run."""

    def __init__(
        self,
        config: AssistantConfig,
        *,
        http_client: httpx.Client | None = None,
        spawn: Callable[..., subprocess.Popen] | None = None,
        port_open: Callable[[str, int], bool] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.config = config
        self._scheme, self.host, self.port = split_base_url(config.llama_base_url)
        self.health_url = health_url_for(config.llama_base_url)
        self._http = http_client or httpx.Client(timeout=httpx.Timeout(2.0), trust_env=False)
        self._spawn = spawn or subprocess.Popen
        self._port_open = port_open or port_is_open
        self._sleep = sleep or time.sleep
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._cleanup_registered = False

    # -- public API -----------------------------------------------------------

    def ensure_running(self, progress: ProgressCallback | None = None) -> bool:
        """Make sure the brain is reachable. Return True if this call started it."""
        note = progress or (lambda _msg: None)
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                if self._probe_state()[0] == "ready":
                    return False
                self._wait_until_ready(note)
                return False

            state, detail = self._probe_state()
            if state == "ready":
                note(f"already running at {self._scheme}://{self.host}:{self.port} ({detail}) — reusing it.")
                return False
            if state == "loading":
                note(f"a llama-server is already starting on port {self.port} — waiting for it.")
                self._wait_until_ready(note)
                return False
            if state == "occupied":
                raise LlamaServerError(
                    f"Port {self.port} is used by another program, not by Ultron's llama-server ({detail}). "
                    f"Stop that program, or change LLAMA_BASE_URL in .env. Ultron will not start a second copy."
                )

            self._start(note)
            self._wait_until_ready(note)
            return True

    def stop(self) -> None:
        """Terminate only the server this run started; a reused server is left alone."""
        proc = self._proc
        if proc is None:
            return
        self._proc = None
        if proc.poll() is not None:
            return
        logger.info("Stopping llama-server (pid %s)", getattr(proc, "pid", "?"))
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
            except OSError:
                pass

    def close(self) -> None:
        """Stop the child (if any) and release the HTTP client."""
        self.stop()
        try:
            self._http.close()
        except Exception:  # pragma: no cover - defensive
            pass

    # -- internals ------------------------------------------------------------

    def _probe_state(self) -> tuple[str, str]:
        return _probe(self._http, self.health_url, self.host, self.port, self._port_open)

    def _start(self, note: ProgressCallback) -> None:
        binary = Path(self.config.llama_server_binary).expanduser()
        model = Path(self.config.llama_model_path).expanduser()
        if not binary.is_file():
            raise LlamaServerError(
                f"llama-server binary not found: {binary}. Set LLAMA_SERVER_BIN in .env."
            )
        log_path = Path(self.config.llama_server_log).expanduser()
        log_path.parent.mkdir(parents=True, exist_ok=True)

        # Last-second re-check so we never race a just-started manual server.
        if self._port_open(self.host, self.port):
            raise LlamaServerError(
                f"Port {self.port} became busy while starting. Ultron will not start a second copy."
            )

        cmd = [
            str(binary),
            "-m",
            str(model),
            *SERVER_FLAGS,
            "--host",
            self.host,
            "--port",
            str(self.port),
        ]
        if not model.is_file():
            raise LlamaServerError(
                f"Local model file not found: {model}. Set LLAMA_MODEL_PATH in .env."
            )

        logger.info("Starting llama-server: %s (log: %s)", " ".join(cmd), log_path)
        note(f"not running — starting it (model load takes ~25 s, log: {log_path})")
        try:
            with open(log_path, "ab", buffering=0) as handle:
                self._proc = self._spawn(
                    cmd,
                    cwd=str(binary.parent),
                    stdin=subprocess.DEVNULL,
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
        except OSError as exc:
            self._proc = None
            raise LlamaServerError(f"Could not launch llama-server ({binary}): {exc}") from exc

        self._register_cleanup()

    def _wait_until_ready(self, note: ProgressCallback) -> None:
        timeout = max(1.0, float(self.config.llama_start_timeout_sec))
        started = time.monotonic()
        deadline = started + timeout
        next_note = 5.0
        while True:
            state, detail = self._probe_state()
            if state == "ready":
                note(f"ready in {time.monotonic() - started:.1f} s ({detail}).")
                return
            if state == "occupied":
                self.stop()
                raise LlamaServerError(
                    f"Port {self.port} is answering, but it is not a healthy llama-server ({detail})."
                )
            proc = self._proc
            if proc is not None and proc.poll() is not None:
                code = proc.poll()
                self.stop()
                raise LlamaServerError(
                    f"llama-server exited during startup (exit code {code}). Last log lines from "
                    f"{self.config.llama_server_log}:\n{self._log_tail()}"
                )

            elapsed = time.monotonic() - started
            if elapsed >= timeout:
                was_ours = proc is not None
                self.stop()
                raise LlamaServerError(
                    f"llama-server did not become ready within {timeout:.0f} s"
                    f"{' (child stopped)' if was_ours else ''}. See {self.config.llama_server_log}."
                )
            if elapsed >= next_note:
                note(f"still loading... {elapsed:.0f} s")
                next_note += 5.0
            self._sleep(min(0.5, max(0.01, deadline - time.monotonic())))

    def _log_tail(self, limit: int = 800) -> str:
        path = Path(self.config.llama_server_log).expanduser()
        try:
            data = path.read_bytes()[-limit:]
        except OSError:
            return "(no log yet)"
        return data.decode("utf-8", errors="replace").strip() or "(log is empty)"

    def _register_cleanup(self) -> None:
        """Stop the child on normal exit, Ctrl+C, and SIGTERM/SIGHUP."""
        if self._cleanup_registered:
            return
        self._cleanup_registered = True
        atexit.register(self.stop)
        if threading.current_thread() is not threading.main_thread():
            return
        for sig in (signal.SIGTERM, signal.SIGHUP):
            try:
                if signal.getsignal(sig) is signal.SIG_IGN:
                    continue
                signal.signal(sig, self._on_signal)
            except (ValueError, OSError):  # not the main thread, or unsupported platform
                continue

    def _on_signal(self, signum: int, _frame: object) -> None:
        self.stop()
        raise SystemExit(128 + signum)
