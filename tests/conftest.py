"""Shared test setup: keep every test out of the real logs/ and workspace/.

`assistant.config` reads the log and workspace folders at import time, so the redirect
happens here before any test module imports it. An autouse fixture also fails a test that
somehow writes into the real `logs/assistant.log` or `logs/audit.jsonl`.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_SESSION_TMP = Path(tempfile.mkdtemp(prefix="ultron-tests-"))
os.environ.setdefault("ULTRON_LOGS_DIR", str(_SESSION_TMP / "logs"))
os.environ.setdefault("ULTRON_WORKSPACE_DIR", str(_SESSION_TMP / "workspace"))
# A coding session may export the old model names; tests must see the project's .env
# defaults (small.en) so the Phase 8 fallback and decode options are deterministic.
for _shadowed in ("WHISPER_MODEL", "WHISPER_MODEL_DIR"):
    os.environ.pop(_shadowed, None)

import pytest  # noqa: E402  (after the environment redirect on purpose)

from assistant.config import LOGS_DIR, PROJECT_ROOT, WORKSPACE_DIR  # noqa: E402

REAL_LOG_FILES = (
    PROJECT_ROOT / "logs" / "assistant.log",
    PROJECT_ROOT / "logs" / "audit.jsonl",
)


def _snapshot(paths: tuple[Path, ...]) -> tuple[tuple[bool, int, int], ...]:
    rows = []
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            rows.append((False, 0, 0))
        else:
            rows.append((True, stat.st_mtime_ns, stat.st_size))
    return tuple(rows)


@pytest.fixture(scope="session", autouse=True)
def _redirected_logs() -> None:
    assert LOGS_DIR != PROJECT_ROOT / "logs", "tests must not use the real logs/ folder"
    assert WORKSPACE_DIR != PROJECT_ROOT / "workspace", "tests must not use the real workspace/"


@pytest.fixture(autouse=True)
def _real_logs_untouched() -> None:
    """Fail any test that writes into the real log files (tmp redirect must handle it)."""
    before = _snapshot(REAL_LOG_FILES)
    yield
    after = _snapshot(REAL_LOG_FILES)
    assert after == before, (
        "a test wrote into the real logs/ folder; use ULTRON_LOGS_DIR/tmp_path instead"
    )
