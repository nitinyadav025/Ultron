"""Code-enforced Permission Gate. Enforces safe/confirm/blocked tiers, ALLOWED_ROOTS, and modes."""

from __future__ import annotations

import re
import select
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from assistant.config import AssistantConfig

VALID_MODES = ("ask", "auto-safe", "human-only")
VALID_RISK_TIERS = ("safe", "confirm", "blocked")

_BLOCKED_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)\b(setenforce\s+0|disable\s+selinux|/etc/selinux)\b"), "Disabling or modifying SELinux is blocked."),
    (re.compile(r"(?i)\b(systemctl\s+(stop|disable|mask)\s+(firewalld|polkit))\b"), "Disabling firewalld or polkit is blocked."),
    (re.compile(r"(?i)(/etc/sudoers|/etc/systemd/system|/etc/udev/rules\.d|/etc/polkit-1)"), "Editing system security files is blocked."),
    (re.compile(r"(?i)(gnome-keyring|login\.keyring|signons\.sqlite|logins\.json)"), "Reading GNOME Keyring or browser passwords is blocked."),
    (re.compile(r"(?i)(assistant/safety/|permissions\.py|killswitch\.py|redact\.py|audit\.py)"), "Modifying Ultron's safety stack is blocked."),
]

_INJECTION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?i)ignore\s+(all\s+)?(previous|prior)\s+instructions"),
    re.compile(r"(?i)system\s+prompt\s+override"),
    re.compile(r"(?i)do\s+not\s+ask\s+for\s+confirmation"),
]

# Defence in depth for auto-safe mode: even if a tool is mislabelled "safe", any tool whose
# name promises to delete/change/send data or run a command must still ask the user.
_RISKY_TOOL_NAME = re.compile(
    r"(?i)(delete|remove|trash|move|rename|write|append|edit|replace|copy|send|upload|post|share"
    r"|exec|shell|command|script|kill|install|uninstall|chmod|chown)"
)


@dataclass
class GateDecision:
    """Outcome returned by PermissionGate.check()."""

    allowed: bool
    reason: str
    mode: str
    risk_level: str
    human_only_instruction: str = ""
    needs_confirmation: bool = False


@dataclass
class PermissionGate:
    """Plain-code permission gate enforcing safe/confirm/blocked tiers and home/workspace access."""

    config: AssistantConfig
    mode: str = ""
    tainted_by_untrusted: bool = False
    always_allow_set: set[tuple[str, str]] = field(default_factory=set)
    confirm_callback: Callable[[str, int], str] | None = None

    def __post_init__(self) -> None:
        if self.mode not in VALID_MODES:
            # Single source of truth: config.default_mode (auto-safe by default).
            self.mode = self.config.default_mode

    def set_mode(self, new_mode: str) -> str:
        cleaned = new_mode.strip().lower()
        if cleaned not in VALID_MODES:
            raise ValueError(f"Invalid mode '{new_mode}'. Use: {', '.join(VALID_MODES)}")
        self.mode = cleaned
        return self.mode

    def inspect_untrusted_text(self, text: str) -> bool:
        """Flag the current task if untrusted tool output contains prompt injection patterns."""
        for pat in _INJECTION_PATTERNS:
            if pat.search(text):
                self.tainted_by_untrusted = True
                return True
        return False

    def _is_path_allowed(self, raw_path: str, is_write: bool) -> tuple[bool, str, Path]:
        resolved = Path(raw_path).expanduser().resolve()
        if resolved.name == ".env" or (resolved.name.startswith(".env.") and resolved.name != ".env.example"):
            return False, "BLOCKED: Reading or modifying .env is strictly forbidden.", resolved

        safety_dir = (self.config.project_root / "assistant" / "safety").resolve()
        if is_write and (resolved == safety_dir or safety_dir in resolved.parents):
            return False, "BLOCKED: Modifying Ultron's safety stack is forbidden.", resolved

        for sys_dir in self.config.readonly_system_dirs:
            if resolved == sys_dir or sys_dir in resolved.parents:
                if is_write:
                    return False, f"CONFIRM_SYS: Modifying system directory {sys_dir} requires confirmation.", resolved
                return False, f"CONFIRM_OUTSIDE: Reading system directory {sys_dir} requires confirmation.", resolved

        for root in self.config.allowed_roots:
            if resolved == root or root in resolved.parents:
                return True, "Path inside ALLOWED_ROOTS.", resolved

        return False, f"CONFIRM_OUTSIDE: Path '{resolved}' is outside ALLOWED_ROOTS.", resolved

    def _check_blocked(self, tool_name: str, arguments: dict[str, Any]) -> str | None:
        serialized = f"{tool_name} " + " ".join(f"{k}={v}" for k, v in arguments.items())
        for pat, reason in _BLOCKED_PATTERNS:
            if pat.search(serialized):
                return reason
        for key in ("path", "directory", "target", "source", "destination", "file_path"):
            if key in arguments and isinstance(arguments[key], str):
                p = Path(arguments[key]).expanduser().resolve()
                if p.name == ".env":
                    return "Accessing .env is blocked by the Secrets rule."
                if tool_name in ("delete_file", "trash_file", "remove_file"):
                    for sys_dir in self.config.readonly_system_dirs:
                        if p == sys_dir or sys_dir in p.parents:
                            return f"Deleting system path '{sys_dir}' is blocked."
        return None

    def _prompt_user(self, detail: str) -> str:
        timeout = self.config.confirmation_timeout_sec
        if self.confirm_callback is not None:
            return self.confirm_callback(detail, timeout).strip().lower()
        if not sys.stdin or not sys.stdin.isatty():
            return "no"
        print(f"\n[PERMISSION GATE] Confirm exact action ({timeout}s timeout, defaults to 'no'):")
        print(f"  {detail}")
        print("  Allow? [y/N/always]: ", end="", flush=True)
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        if not ready:
            print("\n  -> Timed out (defaulting to NO).")
            return "no"
        return sys.stdin.readline().strip().lower() or "no"

    def check(
        self,
        tool_name: str,
        risk_level: str,
        read_only: bool,
        arguments: dict[str, Any],
        action_preview: str | None = None,
        allow_prompt: bool = True,
    ) -> GateDecision:
        """Evaluate whether a requested tool call is allowed, needs confirmation, or is blocked.

        ``allow_prompt=False`` is for hands-free mode: it never reads the keyboard, and a
        call that would have asked returns ``needs_confirmation=True`` instead.
        """
        if risk_level not in VALID_RISK_TIERS:
            return GateDecision(False, f"Invalid risk tier '{risk_level}'.", self.mode, risk_level)
        if risk_level == "blocked":
            return GateDecision(False, f"Tool '{tool_name}' is blocked.", self.mode, "blocked")

        blocked_reason = self._check_blocked(tool_name, arguments)
        if blocked_reason:
            return GateDecision(False, f"Refused (Blocked Tier): {blocked_reason}", self.mode, "blocked")

        folder_scope = str(self.config.project_root)
        outside_reason = ""
        for key in ("path", "directory", "target", "source", "destination", "file_path"):
            if key in arguments and isinstance(arguments[key], str):
                ok, msg, resolved = self._is_path_allowed(arguments[key], is_write=not read_only)
                folder_scope = str(resolved.parent if resolved.suffix else resolved)
                if msg.startswith("BLOCKED:"):
                    return GateDecision(False, msg, self.mode, "blocked")
                if not ok:
                    outside_reason = msg

        exact_desc = action_preview or f"{tool_name}({', '.join(f'{k}={v!r}' for k, v in arguments.items())})"
        if self.mode == "human-only" and not read_only:
            inst = f"[HUMAN-ONLY MODE] Perform this action manually: {exact_desc}"
            return GateDecision(False, inst, self.mode, risk_level, human_only_instruction=inst)

        needs_confirm = False
        reason_note = ""
        if risk_level == "confirm":
            needs_confirm, reason_note = True, "confirm-tier tool"
        elif outside_reason:
            needs_confirm, reason_note = True, outside_reason
        elif self.tainted_by_untrusted and not read_only:
            needs_confirm, reason_note = True, "follows reading untrusted content"
        elif not read_only and _RISKY_TOOL_NAME.search(tool_name):
            needs_confirm, reason_note = True, "auto-safe: changes data, moves files, or runs a command"
        elif self.mode == "ask" and not read_only:
            needs_confirm, reason_note = True, "ask mode (confirm beyond read-only)"

        if not needs_confirm:
            return GateDecision(True, "Allowed automatically by safety policy.", self.mode, risk_level)

        if (tool_name, folder_scope) in self.always_allow_set and not self.tainted_by_untrusted:
            return GateDecision(True, f"Allowed by session 'always allow' in {folder_scope}.", self.mode, risk_level)

        if not allow_prompt:
            return GateDecision(
                False,
                f"Needs confirmation, which hands-free mode cannot ask: {exact_desc}",
                self.mode,
                risk_level,
                needs_confirmation=True,
            )

        ans = self._prompt_user(f"{exact_desc}  [{reason_note}]")
        if ans in ("y", "yes"):
            return GateDecision(True, "Approved by user.", self.mode, risk_level)
        if ans in ("a", "always"):
            self.always_allow_set.add((tool_name, folder_scope))
            return GateDecision(True, f"Approved (always allow in {folder_scope}).", self.mode, risk_level)
        return GateDecision(False, "Denied by user (or timed out).", self.mode, risk_level)
