Before anything else, read `docs/ULTRON_PROJECT_BIBLE.md` (the complete project history,
architecture and failure catalogue), then `docs/PROGRESS.md` and `docs/LEFT_WORK.md`, and
continue from the checkpoint / NEXT STEP recorded there.

# Ultron — Local Voice Assistant

Ultron is a Fedora 45 voice-controlled laptop assistant (Python, `~/Downloads/ULTRON`)
being migrated from the Gemini cloud stack to a fully local one: llama.cpp LFM3-1.7B brain
(`http://127.0.0.1:9931/v1`), faster-whisper ears, Piper mouth. The GTX 1650 (4 GB) is reserved
for the LLM only; ears and mouth run on CPU.

**At the start of every session, read `docs/ULTRON_PROJECT_BIBLE.md` first, then
`docs/ULTRON_LOCAL_HANDOFF.md`.**

## Rules
- Do ONE phase at a time, then STOP and tell the user exactly how to test it.
- Use the project venv only: `.venv/bin/python` (never the system `python3`).
- Fedora 45 ships Python 3.15, but onnxruntime/av have no cp315 wheels. The venv is built
  on Python **3.14.9** from `~/.local/bin/python3.14` (`uv python install 3.14`). Rebuild
  the venv only with that interpreter.
- Never run sudo/dnf. Give the exact command and wait for approval.
- Do not download any other language model. Do not add powerful new tools.
- Keep the Gemini rollback working (`BACKEND=gemini`). Default `BACKEND=local` must need no API key.
- Never print, echo, or commit secrets (`.env`).
- Keep the system prompt and tool list short; locally cap at 3 tool rounds per task.
- Update `tests/` so `.venv/bin/python -m pytest` passes.
