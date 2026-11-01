"""Phase 5A: local replies stay one short plain sentence and tool output stays small."""

from __future__ import annotations

from typing import Any

from google.genai import types

from assistant.brain.agent_loop import LOCAL_MAX_TOKENS_FIRST, LOCAL_MAX_TOKENS_FOLLOWUP, AgentLoop
from assistant.brain.gemini_client import TokenUsageStats
from assistant.brain.local_client import LocalUsageMeter
from assistant.brain.prompts import build_system_prompt
from assistant.brain.reply_style import clean_model_text, plain_text, short_plain_reply
from assistant.brain.tool_output import compact_tool_result
from assistant.brain.tool_replies import (
    code_reply_for_tool,
    fallback_reply,
    is_lead_in,
    list_files_reply,
    mute_audio_reply,
    volume_reply,
)
from assistant.config import load_config
from assistant.safety.killswitch import WatchdogMonitor
from assistant.safety.permissions import PermissionGate
from assistant.tools.files import list_files
from assistant.tools.registry import ToolRegistry


def _listing(count: int = 60) -> dict[str, Any]:
    items = [
        {
            "name": f"file{i}.mp4",
            "path": f"/home/nitin/Downloads/file{i}.mp4",
            "type": "file",
            "size_bytes": 9_000_000_000_000 // (i + 1),  # the "9 TB" trap
            "modified": "2026-10-31T10:00:00+00:00",
        }
        for i in range(count)
    ]
    return {"ok": True, "directory": "/home/nitin/Downloads", "count": count, "items": items}


BAD_NAME_REPLY = (
    "I am Fedora GNOME voice assistant, Ultron."
    "< name is Ultron, the Fedora GNOME voice assistant."
)


def test_garbled_name_reply_loses_the_stray_fragment_and_repetition() -> None:
    cleaned = clean_model_text(BAD_NAME_REPLY)

    assert "<" not in cleaned
    assert short_plain_reply(BAD_NAME_REPLY) == "I am Fedora GNOME voice assistant, Ultron."


def test_special_tokens_and_immediately_repeated_sentences_are_removed() -> None:
    assert clean_model_text("Hello <|im_end|> world") == "Hello world"
    assert clean_model_text("Use </s> tags") == "Use tags"
    assert clean_model_text("Done. Done.") == "Done."


def test_plain_text_strips_markdown_lists_and_sizes() -> None:
    reply = (
        "## Downloads\n"
        "**43 items** found in `~/Downloads`:\n"
        "- video.mp4 (9 TB)\n"
        "- notes.txt (1.2 MB)\n"
    )
    cleaned = plain_text(reply)
    assert "#" not in cleaned and "*" not in cleaned and "- " not in cleaned
    assert "TB" not in cleaned and "MB" not in cleaned and "~/" not in cleaned


def test_short_plain_reply_is_one_sentence_and_capped() -> None:
    long = (
        "Here is a **very long** answer: first bullet, second bullet, third bullet. "
        "This second sentence must be dropped entirely because only one is spoken."
    )
    reply = short_plain_reply(long)
    assert reply.count(". ") == 0
    assert len(reply) <= 121
    assert "*" not in reply and ":" not in reply

    tiny = short_plain_reply("Sure thing, opening Firefox now", max_chars=20)
    assert len(tiny) <= 21


def test_compact_listing_keeps_names_and_a_count_only() -> None:
    text = compact_tool_result("list_files", {"ok": True, "raw_summary": _listing(60)})
    assert text.startswith('<untrusted_tool_output tool="list_files">')
    assert "60 item(s)" in text
    assert "file0.mp4" in text
    assert "file14.mp4" in text  # 15 entries shown
    assert "file15.mp4" not in text
    assert "size" not in text.lower() and "TB" not in text
    assert "/home/nitin" not in text
    assert "… 45 more not shown" in text


def test_compact_read_file_truncates_content() -> None:
    payload = {"ok": True, "raw_summary": {"ok": True, "path": "/home/x/a.txt", "size_bytes": 999, "content": "A" * 2000}}
    text = compact_tool_result("read_file", payload)
    assert len(text) < 800
    assert "A" * 400 in text
    assert "size_bytes" not in text


def test_compact_error_is_short_and_fenced() -> None:
    text = compact_tool_result("open_app", {"ok": False, "error": "Executable 'x' not found."})
    assert "error:" in text
    assert text.endswith("</untrusted_tool_output>")


class FakeLocalAdapter:
    """Returns one tool call, then a long formatted reply (the Phase 5A bug)."""

    is_local = True
    usage_meter = LocalUsageMeter()

    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    def select_model(self, role: str = "main") -> str:
        return "fake-local"

    def generate_turn(
        self,
        contents: object,
        system_instruction: str,
        tools: list[dict[str, Any]] | None = None,
        role: str = "main",
        thinking_level: str | None = None,
        max_retries: int = 1,
        max_output_tokens: int | None = None,
    ) -> tuple[types.GenerateContentResponse, TokenUsageStats]:
        self.payloads.append({"tools": tools, "max_tokens": max_output_tokens, "contents": contents})
        if len(self.payloads) == 1:
            content = types.Content(
                role="model",
                parts=[types.Part(function_call=types.FunctionCall(name="list_files", args={"directory": "~/Downloads"}, id="c1"))],
            )
        else:
            content = types.Content(
                role="model",
                parts=[
                    types.Part.from_text(
                        text="### Files\n- **video.mp4** (9 TB)\n- notes.txt (1.2 MB)\n\nAll 43 files are listed above."
                    )
                ],
            )
        return types.GenerateContentResponse(candidates=[types.Candidate(content=content)]), TokenUsageStats()


def test_local_turn_sends_compact_tool_output_and_one_sentence_reply() -> None:
    cfg = load_config()
    cfg.backend = "local"
    cfg.voice_reply_max_chars = 120
    gate = PermissionGate(config=cfg, mode="auto-safe", confirm_callback=lambda _d, _t: "no")
    registry = ToolRegistry(config=cfg, gate=gate)
    registry.register(list_files)
    watchdog = WatchdogMonitor(cfg.max_steps, cfg.wall_clock_timeout_sec, cfg.idle_standdown_sec)
    adapter = FakeLocalAdapter()
    loop = AgentLoop(cfg, adapter, registry, watchdog)

    result = loop.run_task("list files in my Downloads folder")

    # The tool result handed back to the model carries names + a count, never sizes.
    payload = None
    for content in adapter.payloads[1]["contents"]:
        for part in getattr(content, "parts", None) or []:
            if getattr(part, "function_response", None) is not None:
                payload = part.function_response.response
    assert payload is not None
    assert payload["ok"] is True
    assert "size" not in str(payload).lower()
    assert len(str(payload)) < 800

    # The final spoken reply is built from the tool result, one plain sentence, no sizes.
    assert result.reply.startswith("There are ")
    assert result.reply.endswith("in your Downloads folder.")
    assert result.reply.count("item") == 1
    assert "TB" not in result.reply and "#" not in result.reply and "*" not in result.reply
    assert len(result.reply) <= cfg.voice_reply_max_chars + 1
    # Generation is capped locally: 128 tokens first, 96 after the tool ran.
    assert [p["max_tokens"] for p in adapter.payloads] == [LOCAL_MAX_TOKENS_FIRST, LOCAL_MAX_TOKENS_FOLLOWUP]


BAD_LOCAL_REPLY = "Here are the first items in your download folder."


def test_list_files_reply_uses_the_real_count_and_folder() -> None:
    assert list_files_reply({"ok": True, "raw_summary": _listing(0)}) == "There are no items in your Downloads folder."
    assert list_files_reply({"ok": True, "raw_summary": _listing(1)}) == "There is 1 item in your Downloads folder."
    assert list_files_reply({"ok": True, "raw_summary": _listing(43)}) == "There are 43 items in your Downloads folder."


def test_list_files_reply_is_skipped_for_a_failed_result() -> None:
    assert list_files_reply({"ok": False, "error": "Directory not found"}) == "I couldn't find that folder."


def test_bad_downloads_lead_in_is_replaced_by_a_code_reply() -> None:
    assert is_lead_in(BAD_LOCAL_REPLY) is True

    cfg = load_config()
    cfg.backend = "local"
    gate = PermissionGate(config=cfg, mode="auto-safe", confirm_callback=lambda _d, _t: "no")
    registry = ToolRegistry(config=cfg, gate=gate)
    watchdog = WatchdogMonitor(cfg.max_steps, cfg.wall_clock_timeout_sec, cfg.idle_standdown_sec)
    loop = AgentLoop(cfg, FakeLocalAdapter(), registry, watchdog)

    reply = loop._finalize_reply(BAD_LOCAL_REPLY, ("list_files", {"ok": True, "raw_summary": _listing(7)}))
    assert reply == "There are 7 items in your Downloads folder."

    # Any other tool falls back to its own short template instead of the opener.
    assert loop._finalize_reply(BAD_LOCAL_REPLY, ("read_file", {"ok": True})) == "I read the file."


def test_lead_in_fallback_is_a_plain_sentence_per_tool() -> None:
    assert is_lead_in("Sure, here you go.") is True
    assert is_lead_in("There are 43 items in your Downloads folder.") is False
    assert fallback_reply("list_files") == "I listed the folder."
    assert fallback_reply("read_file") == "I read the file."
    assert fallback_reply("volume_up") == "Done."


def test_mute_and_volume_replies_come_from_the_tool_result() -> None:
    assert mute_audio_reply({"ok": True, "raw_summary": {"ok": True, "muted": True}}) == "Audio is now muted."
    assert mute_audio_reply({"ok": True, "raw_summary": {"ok": True, "muted": False}}) == "Audio is now unmuted."
    assert mute_audio_reply({"ok": False, "error": "wpctl failed"}) == "I couldn't change the audio."

    assert volume_reply({"ok": True, "raw_summary": {"ok": True, "requested_percent": 30}}) == (
        "Volume is now set to 30 percent."
    )
    assert volume_reply({"ok": True, "raw_summary": {"ok": True, "volume_percent": 80}}) == (
        "Volume is now set to 80 percent."
    )
    assert volume_reply({"ok": False, "error": "wpctl failed"}) == "I couldn't set the volume."
    assert code_reply_for_tool("mute_audio", {"ok": True, "raw_summary": {"muted": True}}) == (
        "Audio is now muted."
    )


def test_live_timed_mute_reply_is_replaced_by_the_real_action() -> None:
    cfg = load_config()
    cfg.backend = "local"
    gate = PermissionGate(config=cfg, mode="auto-safe", confirm_callback=lambda _d, _t: "no")
    registry = ToolRegistry(config=cfg, gate=gate)
    watchdog = WatchdogMonitor(cfg.max_steps, cfg.wall_clock_timeout_sec, cfg.idle_standdown_sec)
    loop = AgentLoop(cfg, FakeLocalAdapter(), registry, watchdog)

    # The live build said "Audio is muted for 10 seconds." although no timer exists.
    reply = loop._finalize_reply(
        "Audio is muted for 10 seconds.",
        ("mute_audio", {"ok": True, "raw_summary": {"ok": True, "muted": True}}),
    )
    assert reply == "Audio is now muted."


def test_local_prompt_is_short_and_static_prefix_comes_first() -> None:
    cfg = load_config()
    cfg.backend = "local"
    prompt = build_system_prompt(cfg, "auto-safe")
    assert len(prompt) < 320
    assert prompt.startswith("Ultron:")
    # Anything volatile (mode, roots, date) sits at the very end for prompt-cache reuse.
    assert prompt.rstrip().splitlines()[-1].startswith("Ctx:")
    other = build_system_prompt(cfg, "human-only")
    first_line = prompt.splitlines()[0]
    assert other.splitlines()[0] == first_line
    # The home root already covers Desktop/Documents/Downloads, so it is not repeated.
    assert prompt.count("~/Desktop") == 0
