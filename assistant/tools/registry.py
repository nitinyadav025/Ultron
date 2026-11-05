"""Automatic Gemini FunctionDeclaration generator and tool registry."""

from __future__ import annotations

import inspect
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, get_type_hints

from google.genai import types

from assistant.config import AssistantConfig
from assistant.safety.audit import log_audit_event
from assistant.safety.permissions import PermissionGate
from assistant.safety.redact import redact_secrets

logger = logging.getLogger("ultron.tools")


@dataclass
class ToolSpec:
    """Metadata for a registered tool function."""

    name: str
    description: str
    risk_level: str
    read_only: bool
    func: Callable[..., Any]
    declaration: types.FunctionDeclaration


_PY_TYPE_TO_SCHEMA: dict[Any, types.Type] = {
    str: types.Type.STRING,
    int: types.Type.INTEGER,
    float: types.Type.NUMBER,
    bool: types.Type.BOOLEAN,
    list: types.Type.ARRAY,
    dict: types.Type.OBJECT,
}

# Terse parameter hints for the OpenAI schemas: the local 1.7B model pays for every token
# it re-reads, and "Parameter 'directory' (default: '.')" said nothing a model could use.
_PARAM_HINTS: dict[str, str] = {
    "directory": "folder path",
    "pattern": "glob filter",
    "recursive": "include subfolders",
    "path": "file path",
    "max_chars": "read limit",
    "step_percent": "5-20 step",
    "percent": "0-100 target",
    "muted": "true=mute",
    "trigger": "spoken phrase",
    "actions": "actions separated by ;",
}

_SCHEMA_TYPE_TO_JSON: dict[Any, str] = {
    types.Type.STRING: "string",
    types.Type.INTEGER: "integer",
    types.Type.NUMBER: "number",
    types.Type.BOOLEAN: "boolean",
    types.Type.ARRAY: "array",
    types.Type.OBJECT: "object",
}

# Local 1.7B schemas: short descriptions. The model re-reads this list on every turn.
# The fast path handles the other tools deterministically, so the LLM only sees the ones
# it genuinely needs for an unknown phrasing. Measured in tests/test_b6_smart_brain.py.
_COMPACT_DESCRIPTIONS: dict[str, str] = {
    "list_files": "List a folder.",
    "read_file": "Read a file.",
    "open_app": "Open an app or web app.",
    "close_app": "Close a running app.",
    "switch_app": "Focus an app.",
    "volume_up": "Turn volume up.",
    "volume_down": "Turn volume down.",
    "set_volume": "Set volume percent.",
    "mute_audio": "Mute or unmute.",
    "web_search": "Search the web.",
    "open_first_result": "Open first web result.",
    "type_text": "Type text in the focused window.",
    "press_key": "Press enter, tab, escape, arrows or page keys.",
    "scroll_page": "Scroll the page.",
    "browser_tab_control": "Control browser tabs.",
    "window_control": "Control windows.",
    "set_brightness": "Set screen brightness.",
    "media_control": "Play, pause or skip media.",
    "system_info": "Read battery, date, time or status.",
    "take_screenshot": "Take a screenshot.",
    "set_timer": "Set a countdown timer.",
    "learn_skill": "Learn a phrase and its actions.",
    "click_element": "Click a visible button, link or tab.",
    "read_screen": "Read what is on the screen.",
    "focus_and_type": "Focus an input field and type text.",
}

# Phase 7 exposes EVERY registered desktop tool to the local model. The fast path still
# handles common whole sentences first, and the static system prompt is cached once.
LOCAL_SCHEMA_TOOLS = frozenset()

# Optional parameters the local model may genuinely need. Everything else optional is
# omitted: the Python default is still used when the model leaves it out.
_LOCAL_OPTIONAL_PARAMS: dict[str, frozenset[str]] = {
    "list_files": frozenset({"pattern"}),
    "read_file": frozenset({"max_chars"}),
    "web_search": frozenset({"browser", "site", "in_current_tab"}),
    "open_first_result": frozenset({"browser", "site", "first_video"}),
    "type_text": frozenset({"press_enter"}),
    "press_key": frozenset(),
    "scroll_page": frozenset({"direction", "amount"}),
    "set_volume": frozenset({"percent"}),
    "mute_audio": frozenset({"muted"}),
    "set_brightness": frozenset({"percent", "delta"}),
    "media_control": frozenset({"action"}),
    "system_info": frozenset({"kind"}),
    "set_timer": frozenset({"label"}),
    "read_screen": frozenset({"query"}),
    "focus_and_type": frozenset(),
}


def _build_declaration(func: Callable[..., Any], name: str, description: str) -> types.FunctionDeclaration:
    sig = inspect.signature(func)
    hints = get_type_hints(func)
    properties: dict[str, types.Schema] = {}
    required: list[str] = []

    for param_name, param in sig.parameters.items():
        if param_name in ("self", "cls"):
            continue
        ann = hints.get(param_name, str)
        origin = getattr(ann, "__origin__", None)
        if origin is not None:
            args = [a for a in getattr(ann, "__args__", ()) if a is not type(None)]
            ann = args[0] if args else str
        schema_type = _PY_TYPE_TO_SCHEMA.get(ann, types.Type.STRING)
        is_required = param.default is inspect.Parameter.empty
        # Only required params get a hint: an omitted optional param simply uses its
        # default, so describing it would only cost the small local model tokens.
        hint = _PARAM_HINTS.get(param_name, "") if is_required else ""
        properties[param_name] = types.Schema(type=schema_type, description=hint or None)
        if is_required:
            required.append(param_name)

    return types.FunctionDeclaration(
        name=name,
        description=description,
        parameters=types.Schema(type=types.Type.OBJECT, properties=properties, required=required),
    )


def tool(risk_level: str = "safe", read_only: bool = True, name: str | None = None) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator marking a typed Python function as an Ultron tool."""
    if risk_level not in ("safe", "confirm", "blocked"):
        raise ValueError(f"Invalid risk_level: {risk_level}")

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        tool_name = name or func.__name__
        doc = (inspect.getdoc(func) or f"Execute {tool_name}").strip()
        decl = _build_declaration(func, tool_name, doc)
        setattr(func, "_ultron_tool_spec", ToolSpec(tool_name, doc, risk_level, read_only, func, decl))
        return func

    return decorator


def wrap_untrusted_output(tool_name: str, payload: Any, max_chars: int = 4000) -> str:
    """Truncate long tool output and wrap in explicit untrusted-data delimiters."""
    raw = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, indent=2)
    if len(raw) > max_chars:
        raw = raw[:max_chars] + "\n...[truncated for context limit]..."
    return f'<untrusted_tool_output tool="{tool_name}">\n{raw}\n</untrusted_tool_output>'


class ToolRegistry:
    """Holds registered tools, builds Gemini Tool schemas, and routes execution through PermissionGate."""

    def __init__(self, config: AssistantConfig, gate: PermissionGate, allow_prompts: bool = True) -> None:
        self.config = config
        self.gate = gate
        self._tools: dict[str, ToolSpec] = {}
        # Hands-free mode sets this False so a tool never reads the keyboard.
        self.allow_prompts = allow_prompts
        self.needs_confirmation_seen = False
        self.on_execute: Callable[[str], None] | None = None

    def register(self, func: Callable[..., Any]) -> ToolSpec:
        spec: ToolSpec | None = getattr(func, "_ultron_tool_spec", None)
        if spec is None:
            raise ValueError(f"Function {func.__name__} is missing the @tool decorator.")
        self._tools[spec.name] = spec
        return spec

    def list_tools(self) -> list[ToolSpec]:
        return list(self._tools.values())

    def get_gemini_tools(self, include_google_search: bool = False) -> list[types.Tool]:
        decls = [s.declaration for s in self._tools.values()]
        out: list[types.Tool] = [types.Tool(function_declarations=decls)] if decls else []
        if include_google_search and self.config.paid_tier:
            out.append(types.Tool(google_search=types.GoogleSearch()))
        return out

    def get_openai_tools(self, compact: bool = False) -> list[dict[str, Any]]:
        """Build OpenAI-compatible function schemas from the registered tools (for llama.cpp).

        ``compact=True`` is the local 1.7B path: only the 12 tools that still need a model
        round, short descriptions, required params plus the few optional ones the model
        really uses. The default keeps every tool/param for Gemini and for tests.
        """
        out: list[dict[str, Any]] = []
        for spec in self._tools.values():
            decl = spec.declaration
            if compact and LOCAL_SCHEMA_TOOLS and decl.name not in LOCAL_SCHEMA_TOOLS:
                continue
            parameters = decl.parameters
            required = list(parameters.required or []) if parameters else []
            keep_optional = _LOCAL_OPTIONAL_PARAMS.get(decl.name, frozenset()) if compact else None
            properties: dict[str, Any] = {}
            for prop_name, prop in (parameters.properties or {}).items() if parameters else []:
                if keep_optional is not None and prop_name not in required and prop_name not in keep_optional:
                    continue
                prop_schema: dict[str, Any] = {"type": _SCHEMA_TYPE_TO_JSON.get(prop.type, "string")}
                if prop.description:
                    prop_schema["description"] = prop.description
                properties[prop_name] = prop_schema
            function: dict[str, Any] = {"name": decl.name}
            if properties or not compact:
                param_schema: dict[str, Any] = {"type": "object", "properties": properties}
                if required:
                    param_schema["required"] = required
                function["parameters"] = param_schema
            description = _COMPACT_DESCRIPTIONS.get(decl.name, decl.description) if compact else decl.description
            if description:
                function["description"] = description
            out.append({"type": "function", "function": function})
        return out

    def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        user_request: str = "",
        dry_run: bool = False,
        action_preview: str | None = None,
    ) -> dict[str, Any]:
        start = time.perf_counter()
        # One INFO line per call so a live bug can be traced to the exact arguments.
        logger.info(
            "Tool call: %s(%s)",
            tool_name,
            redact_secrets(json.dumps(arguments, ensure_ascii=False, default=str)),
        )
        spec = self._tools.get(tool_name)
        if spec is None:
            return {"ok": False, "error": f"Unknown tool '{tool_name}'."}
        if tool_name == "type_text" and "terminal" in (user_request or "").lower():
            # The user explicitly wants the terminal: do not move focus away from it.
            try:
                from assistant.tools.desktop_control import note_explicit_terminal

                note_explicit_terminal()
            except Exception:
                pass
        if dry_run:
            return {"ok": True, "dry_run": True, "planned_call": f"{tool_name}({arguments})", "risk_level": spec.risk_level}

        decision = self.gate.check(
            spec.name,
            spec.risk_level,
            spec.read_only,
            arguments,
            action_preview,
            allow_prompt=self.allow_prompts,
        )
        if not decision.allowed:
            elapsed = (time.perf_counter() - start) * 1000.0
            res = {
                "ok": False,
                "denied": True,
                "reason": decision.reason,
                "human_only_instruction": decision.human_only_instruction,
            }
            if decision.needs_confirmation:
                self.needs_confirmation_seen = True
                res["needs_confirmation"] = True
            log_audit_event(user_request, spec.name, arguments, spec.risk_level, self.gate.mode, False, res, elapsed)
            return res

        if self.on_execute is not None:
            self.on_execute(spec.name)
        try:
            raw_out = spec.func(**arguments)
            self.gate.inspect_untrusted_text(json.dumps(raw_out, ensure_ascii=False))
            wrapped = wrap_untrusted_output(spec.name, raw_out, self.config.max_tool_output_chars)
            payload = {"ok": True, "data": wrapped, "raw_summary": raw_out}
        except Exception as exc:
            payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

        elapsed = (time.perf_counter() - start) * 1000.0
        log_audit_event(user_request, spec.name, arguments, spec.risk_level, self.gate.mode, True, payload.get("raw_summary", payload.get("error")), elapsed)
        return payload
