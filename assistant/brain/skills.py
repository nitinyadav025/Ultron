"""Trainable Ultron: learned voice skills and a privacy-safe experience log.

A skill is a trigger phrase plus an ordered action chain, for example::

    {"trigger": "work mode", "actions": ["open code", "open github"]}

Skills live in two JSON files:

* ``config/learned_skills.json`` — shipped defaults, committed;
* ``workspace/learned_skills.json`` — what the user taught, git-ignored, wins on conflict.

Teaching works by voice ("remember when I say work mode, open code and open github") and
by CLI (``main.py teach "work mode" "open code; open github"``). The experience log only
stores accepted wake commands and their tool names — never audio, secrets or ignored
speech.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from assistant.config import WORKSPACE_DIR, AssistantConfig, load_config
from assistant.safety.redact import redact_secrets
from assistant.tools.registry import tool

logger = logging.getLogger("ultron.skills")

SKILL_FILE_VERSION = 1
_ACTION_VERBS = (
    "open",
    "launch",
    "close",
    "quit",
    "search",
    "type",
    "write",
    "scroll",
    "switch",
    "play",
    "pause",
    "set",
    "turn",
    "mute",
    "unmute",
    "list",
    "read",
    "take",
    "focus",
    "click",
    "show",
    "start",
    "go",
)
_ACTION_SPLIT = re.compile(
    r"\s*;\s*|\s+and\s+(?=(?:" + "|".join(_ACTION_VERBS) + r")\b)", re.I
)
_TEACH_PATTERNS = (
    re.compile(
        r"^(?:ultron[ ,]+)?(?:please\s+)?remember\s+(?:that\s+)?(?:when\s+i\s+say|when\s+i\s+ask)\s+"
        r"(?P<trigger>.+?)(?:\s*,\s*|\s+means\s+|\s+then\s+)(?P<actions>.+)$",
        re.I,
    ),
    re.compile(
        r"^(?:ultron[ ,]+)?(?:please\s+)?learn\s+(?:a\s+|the\s+)?skill\s+"
        r"(?P<trigger>.+?)\s+(?:means|to|->)\s+(?P<actions>.+)$",
        re.I,
    ),
    re.compile(
        r"^(?:ultron[ ,]+)?(?:please\s+)?when\s+i\s+say\s+"
        r"(?P<trigger>.+?)(?:\s*,\s*|\s+means\s+)(?P<actions>.+)$",
        re.I,
    ),
)

_MEMORY_ALIAS_PATTERN = re.compile(
    r"^(?:ultron[ ,]+)?(?:please\s+)?remember\s+(?:that\s+)?(?P<alias>.+?)\s+means\s+(?P<target>.+)$",
    re.I,
)
_MEMORY_FACT_PATTERN = re.compile(
    r"^(?:ultron[ ,]+)?(?:please\s+)?remember\s+(?:that\s+)?(?P<key>my\s+.+?)\s+(?:is|are)\s+(?P<value>.+)$",
    re.I,
)
_CHATML_SYSTEM = (
    "You are Ultron, a sharp, warm, local Fedora GNOME voice assistant. "
    "Reply in one or two complete spoken sentences and use tools when an action is needed."
)

_ACTIVE_CONFIG: AssistantConfig | None = None


@dataclass(frozen=True)
class LearnedSkill:
    trigger: str
    actions: tuple[str, ...]
    source: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"trigger": self.trigger, "actions": list(self.actions)}


def configure_runtime(config: AssistantConfig) -> None:
    """Remember the process config so the learn_skill tool writes the right workspace."""
    global _ACTIVE_CONFIG
    _ACTIVE_CONFIG = config


def _config(config: AssistantConfig | None = None) -> AssistantConfig:
    return config or _ACTIVE_CONFIG or load_config()


def normalise(text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", " ", (text or "").lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def split_actions(text: str) -> tuple[str, ...]:
    """Split "open code; open github" or "open code and open github" into actions."""
    parts = [part.strip(" .") for part in _ACTION_SPLIT.split(text or "") if part.strip(" .")]
    return tuple(dict.fromkeys(part for part in parts if part))


def _memory_file(config: AssistantConfig | None = None) -> Path:
    cfg = _config(config)
    return Path(getattr(cfg, "user_memory_file", WORKSPACE_DIR / "user_memory.json")).expanduser()


def load_user_memory(config: AssistantConfig | None = None) -> dict[str, Any]:
    """Read workspace/user_memory.json (facts and spoken aliases); never raises."""
    try:
        data = json.loads(_memory_file(config).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"facts": {}, "aliases": {}}
    if not isinstance(data, dict):
        return {"facts": {}, "aliases": {}}
    facts = data.get("facts") if isinstance(data.get("facts"), dict) else {}
    aliases = data.get("aliases") if isinstance(data.get("aliases"), dict) else {}
    return {"facts": dict(facts), "aliases": dict(aliases)}


def save_user_memory(config: AssistantConfig | None, memory: dict[str, Any]) -> Path:
    """Atomically persist the user memory file."""
    target = _memory_file(config)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": SKILL_FILE_VERSION,
        "facts": dict(memory.get("facts") or {}),
        "aliases": dict(memory.get("aliases") or {}),
    }
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)
    return target


def memory_key(name: str) -> str:
    cleaned = normalise(name)
    return re.sub(r"^(?:my|the)\s+", "", cleaned).replace(" ", "_")


def save_user_fact(config: AssistantConfig | None, key: str, value: str) -> str:
    """Store a fact such as ``email = nitin@example.com``."""
    cleaned_key = memory_key(key)
    cleaned_value = re.sub(r"\s+", " ", value or "").strip().strip(" .")
    if not cleaned_key or not cleaned_value:
        raise ValueError("A memory needs a name and a value.")
    memory = load_user_memory(config)
    memory["facts"][cleaned_key] = cleaned_value
    save_user_memory(config, memory)
    return cleaned_key


def save_user_alias(config: AssistantConfig | None, alias: str, target: str) -> tuple[str, str]:
    """Store ``"loot risk" -> "lutris"`` so a misheard app name resolves later."""
    cleaned_alias = normalise(alias)
    cleaned_target = normalise(target)
    cleaned_target = re.sub(r"^(?:open|launch|start|run)\s+", "", cleaned_target).strip()
    if not cleaned_alias or not cleaned_target:
        raise ValueError("An alias needs a spoken name and a target app.")
    memory = load_user_memory(config)
    memory["aliases"][cleaned_alias] = cleaned_target
    save_user_memory(config, memory)
    return cleaned_alias, cleaned_target


def delete_user_fact(config: AssistantConfig | None, key: str) -> bool:
    """Remove a taught fact; return True when it existed."""
    cleaned = memory_key(key)
    memory = load_user_memory(config)
    if cleaned not in memory["facts"]:
        return False
    del memory["facts"][cleaned]
    save_user_memory(config, memory)
    return True


def delete_user_alias(config: AssistantConfig | None, alias: str) -> bool:
    """Remove a taught spoken alias; return True when it existed."""
    cleaned = normalise(alias)
    memory = load_user_memory(config)
    if cleaned not in memory["aliases"]:
        return False
    del memory["aliases"][cleaned]
    save_user_memory(config, memory)
    return True


def delete_user_skill(config: AssistantConfig | None, trigger: str) -> bool:
    """Remove a user-taught workflow (the shipped config skills are read-only)."""
    cfg = _config(config)
    path = Path(cfg.user_skills_file)
    existing = {
        normalise(skill.trigger): skill for skill in _read_skill_file(path, "user")
    }
    key = normalise(trigger)
    if key not in existing:
        return False
    del existing[key]
    _write_skill_file(path, existing.values())
    return True


def runtime_alias(name: str, config: AssistantConfig | None = None) -> str:
    """Resolve a user-taught spoken alias (empty string when there is no match)."""
    cleaned = normalise(name)
    if not cleaned:
        return ""
    return str(load_user_memory(config)["aliases"].get(cleaned) or "")


def memory_value(name: str, config: AssistantConfig | None = None) -> str:
    """Return a stored fact (``"email"``), or '' when it was never taught."""
    cleaned = memory_key(name)
    if not cleaned:
        return ""
    return str(load_user_memory(config)["facts"].get(cleaned) or "")


def parse_memory_teaching(text: str) -> tuple[str, str, str] | None:
    """Parse "remember that <alias> means <app>" or "remember that my email is ...".

    Returns ``("alias", spoken_name, target)`` or ``("fact", key, value)``.
    """
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    if not cleaned:
        return None
    alias_match = _MEMORY_ALIAS_PATTERN.match(cleaned)
    if alias_match:
        alias = alias_match.group("alias").strip(" ,.")
        target = alias_match.group("target").strip(" ,.")
        if alias and target:
            return "alias", alias, target
    fact_match = _MEMORY_FACT_PATTERN.match(cleaned)
    if fact_match:
        key = fact_match.group("key").strip(" ,.")
        value = fact_match.group("value").strip(" ,.")
        if key and value:
            return "fact", key, value
    return None


def parse_teaching_command(text: str) -> tuple[str, tuple[str, ...]] | None:
    """Return (trigger, actions) for a spoken teaching sentence, else None."""
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    if not cleaned:
        return None
    for pattern in _TEACH_PATTERNS:
        match = pattern.match(cleaned)
        if not match:
            continue
        trigger = re.sub(r"\s+", " ", match.group("trigger")).strip(" ,.")
        actions = split_actions(match.group("actions"))
        if trigger and actions:
            return trigger, actions
    return None


def _read_skill_file(path: Path, source: str) -> list[LearnedSkill]:
    try:
        data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    raw = data.get("skills", data) if isinstance(data, dict) else data
    if not isinstance(raw, list):
        return []
    skills: list[LearnedSkill] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        trigger = re.sub(r"\s+", " ", str(item.get("trigger") or "")).strip()
        actions = item.get("actions")
        if isinstance(actions, str):
            actions = list(split_actions(actions))
        if not trigger or not isinstance(actions, list):
            continue
        cleaned_actions = tuple(
            str(action).strip() for action in actions if str(action).strip()
        )
        if cleaned_actions:
            skills.append(LearnedSkill(trigger, cleaned_actions, source=source))
    return skills


def load_skills(config: AssistantConfig | None = None) -> list[LearnedSkill]:
    """Merge shipped and user skills; a user trigger overrides the shipped one."""
    cfg = _config(config)
    merged: dict[str, LearnedSkill] = {}
    for path, source in (
        (Path(cfg.learned_skills_file), "config"),
        (Path(cfg.user_skills_file), "user"),
    ):
        for skill in _read_skill_file(path, source):
            merged[normalise(skill.trigger)] = skill
    return list(merged.values())


def _write_skill_file(path: Path, skills: Iterable[LearnedSkill]) -> None:
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": SKILL_FILE_VERSION,
        "skills": [skill.as_dict() for skill in skills],
    }
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)


def save_user_skill(
    config: AssistantConfig | None, trigger: str, actions: Iterable[str]
) -> LearnedSkill:
    """Persist a user skill (workspace only) and return it."""
    cfg = _config(config)
    cleaned_trigger = re.sub(r"\s+", " ", (trigger or "")).strip(" ,.")
    cleaned_actions = tuple(str(action).strip() for action in actions if str(action).strip())
    if not cleaned_trigger:
        raise ValueError("A skill needs a trigger phrase.")
    if not cleaned_actions:
        raise ValueError("A skill needs at least one action.")
    existing = {
        normalise(skill.trigger): skill
        for skill in _read_skill_file(Path(cfg.user_skills_file), "user")
    }
    skill = LearnedSkill(cleaned_trigger, cleaned_actions, source="user")
    existing[normalise(cleaned_trigger)] = skill
    _write_skill_file(Path(cfg.user_skills_file), existing.values())
    logger.info("Learned skill %r -> %s", cleaned_trigger, "; ".join(cleaned_actions))
    return skill


def import_skills_file(
    config: AssistantConfig | None, path: Path | str
) -> list[LearnedSkill]:
    """Import every skill in a JSON file into the user's workspace file."""
    cfg = _config(config)
    imported: list[LearnedSkill] = []
    for skill in _read_skill_file(Path(path), "import"):
        imported.append(save_user_skill(cfg, skill.trigger, skill.actions))
    return imported


def matching_skills(
    text: str, config: AssistantConfig | None = None, limit: int = 2
) -> list[tuple[float, LearnedSkill]]:
    """Best matches first: exact, prefix/keyword, then fuzzy (>= 0.80)."""
    spoken = normalise(text)
    if not spoken:
        return []
    matches: list[tuple[float, LearnedSkill]] = []
    words = set(spoken.split())
    for skill in load_skills(config):
        trigger = normalise(skill.trigger)
        if not trigger or len(trigger) < 3:
            continue
        score = 0.0
        if spoken == trigger:
            score = 1.0
        elif spoken.startswith(trigger + " ") or spoken.endswith(" " + trigger):
            score = 0.97
        elif trigger in spoken:
            score = 0.94
        else:
            trigger_words = set(trigger.split())
            if trigger_words and trigger_words <= words:
                score = 0.91
            else:
                score = difflib.SequenceMatcher(None, spoken, trigger).ratio()
        if score >= 0.80:
            matches.append((score, skill))
    matches.sort(key=lambda item: item[0], reverse=True)
    return matches[: max(1, limit)]


def match_skill(text: str, config: AssistantConfig | None = None) -> LearnedSkill | None:
    matches = matching_skills(text, config, limit=1)
    return matches[0][1] if matches else None


def prompt_hint(text: str, config: AssistantConfig | None = None) -> str:
    """A compact "Skills:" hint for the LLM, or "" when nothing is relevant."""
    matches = matching_skills(text, config, limit=2)
    if not matches:
        return ""
    rendered = []
    for _score, skill in matches:
        rendered.append(f'"{skill.trigger}" -> {"; ".join(skill.actions)}')
    hint = "Skills: " + ". ".join(rendered) + "."
    return hint[:240]


def log_experience(
    config: AssistantConfig | None,
    *,
    raw_transcript: str,
    repaired_transcript: str,
    tools_called: Iterable[str],
    reply: str,
    latency_ms: float,
    screen_context: dict[str, Any] | None = None,
) -> None:
    """Append one accepted utterance to workspace/training_dataset.jsonl (best effort)."""
    cfg = _config(config)
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "raw_transcript": redact_secrets(raw_transcript or ""),
        "repaired_transcript": redact_secrets(repaired_transcript or ""),
        "screen_context": screen_context or {},
        "tools_called": list(tools_called),
        "reply": redact_secrets(reply or ""),
        "latency_ms": round(float(latency_ms), 1),
    }
    try:
        path = Path(cfg.training_dataset_file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:  # never break a voice turn because the log is unwritable
        logger.warning("Could not write the training dataset: %s", exc)


def export_chatml_dataset(
    config: AssistantConfig | None = None,
    output: Path | str | None = None,
) -> dict[str, Any]:
    """Convert the experience log and learned skills into ChatML JSONL for fine-tuning.

    One JSON object per line, each with a system/user/assistant ``messages`` list. Only
    accepted commands and taught skills are included; secrets are redacted, audio and
    ignored speech never existed in these files. The output is ready for Unsloth/QLoRA
    ``messages`` fine-tuning.
    """
    cfg = _config(config)
    source = Path(cfg.training_dataset_file).expanduser()
    target = Path(output).expanduser() if output else (
        WORKSPACE_DIR / "ultron_finetune_chatml.jsonl"
    )
    target.parent.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, Any]] = []
    try:
        lines = source.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        user = str(record.get("repaired_transcript") or record.get("raw_transcript") or "").strip()
        reply = str(record.get("reply") or "").strip()
        if not user or not reply:
            continue
        tools = [str(name) for name in (record.get("tools_called") or []) if str(name)]
        system = _CHATML_SYSTEM
        if tools:
            system += f" Tools used in this example: {', '.join(tools)}."
        rows.append(
            {
                "messages": [
                    {"role": "system", "content": redact_secrets(system)},
                    {"role": "user", "content": redact_secrets(user)},
                    {"role": "assistant", "content": redact_secrets(reply)},
                ]
            }
        )

    for skill in load_skills(cfg):
        actions = "; ".join(skill.actions)
        rows.append(
            {
                "messages": [
                    {"role": "system", "content": _CHATML_SYSTEM},
                    {"role": "user", "content": redact_secrets(skill.trigger)},
                    {
                        "role": "assistant",
                        "content": redact_secrets(f"Running your saved actions: {actions}."),
                    },
                ]
            }
        )

    temporary = target.with_suffix(target.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    temporary.replace(target)
    return {"written": len(rows), "output": str(target)}


@tool(risk_level="safe", read_only=False)
def learn_skill(trigger: str, actions: str) -> dict[str, Any]:
    """Teach Ultron a phrase and the actions it should run."""
    action_list = split_actions(actions)
    if not trigger.strip() or not action_list:
        return {"ok": False, "error": "I need a trigger phrase and at least one action."}
    try:
        skill = save_user_skill(_ACTIVE_CONFIG, trigger, action_list)
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": f"I couldn't save that skill: {exc}"}
    return {
        "ok": True,
        "trigger": skill.trigger,
        "actions": list(skill.actions),
        "reply": (
            f"I've learned that when you say {skill.trigger}, I should "
            + " and ".join(skill.actions)
            + "."
        ),
    }
