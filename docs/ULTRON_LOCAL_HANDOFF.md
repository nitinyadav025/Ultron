# Ultron — Local Backend Handoff

> No secrets in this document. Nothing from `.env` is copied here.

## a) GOAL and FIXED DECISIONS

Goal: talk to Ultron in English; flow = mic → speech-to-text → **local** LLM picks a tool → Ultron runs it → short spoken reply. No internet, no Gemini calls, no API key for normal use.

Fixed decisions (do not change):
- **Brain:** LFM3-1.7B on llama.cpp `llama-server`, OpenAI-compatible API at `http://127.0.0.1:9931/v1`. Already tested; tool calling works.
- **Do NOT download any other language model.**
- **Ears:** faster-whisper, model `base.en`, CPU, int8.
- **Mouth:** Piper TTS, voice `en_US-lessac-medium`, CPU.
- **GPU:** GTX 1650 4 GB is reserved for the LLM. Ears and mouth run on CPU only.
- Server flags that matter: `-ngl 99 --jinja -c 8192 --temp 0.1 --top-k 50 --repeat-penalty 1.1 --reasoning off --host 127.0.0.1 --port 9931`. `--reasoning off` is essential (~0.5 s replies instead of ~2.5 s).
- Tool calls arrive in `choices[0].message.tool_calls`; `arguments` is a JSON string.

## b) ORIGINAL PROMPT (verbatim, from the conversation)

```text
You are working on "Ultron", my voice-controlled laptop assistant (Python, folder ~/Downloads/assistant-project, Fedora 44). It uses the Gemini API for everything (thinking, speech-to-text, text-to-speech, live voice) and that is too slow. Convert it to run fully locally. Work in phases and STOP after each phase so I can test.

GOAL
I talk to Ultron in English. Flow: mic -> speech-to-text -> local LLM picks a tool -> Ultron runs it -> short spoken reply. No internet, no Gemini calls, no API key needed.

FIXED DECISIONS (do not change)
- Brain: LFM3-1.7B on llama.cpp llama-server, OpenAI-compatible API at http://127.0.0.1:9931/v1. Already tested, tool calling works.
- Do NOT download any other language model.
- Ears: faster-whisper, model base.en, CPU, int8.
- Mouth: Piper TTS, voice en_US-lessac-medium, CPU.
- GPU is a GTX 1650 with 4 GB: reserve it for the LLM. Ears and mouth run on CPU only.

LLM SERVER FACTS
- Binary: ~/Downloads/LLAMA/llama-b12001-bin-ubuntu-cuda-13.4-x64/llama-b12001/llama-server
- Model: ~/Downloads/LFM3-1.7B-Q6_K.gguf
- Command: ./llama-server -m <model> -ngl 99 --jinja -c 8192 --temp 0.1 --top-k 50 --repeat-penalty 1.1 --reasoning off --host 127.0.0.1 --port 9931
- --reasoning off is essential: without it the model thinks first and a reply takes ~2.5 s instead of ~0.5 s.
- Model load takes ~25 s.
- Tool calls arrive in choices[0].message.tool_calls, with arguments as a JSON string.

RULES
- First make a backup (git commit or cp -r) and tell me where it is.
- Keep the Gemini code working behind a config switch (BACKEND=local or gemini), default local, so I can roll back.
- With BACKEND=local, nothing may require a Gemini API key (config.py currently checks the key and model names).
- Install Python packages only into the environment Ultron already uses. If something needs sudo or dnf, do not run it: give me the exact command and wait.
- Check the real API of each installed library (versions differ) before writing code. Do not guess.
- Do not add powerful new tools (e.g. run-any-shell-command). Keep existing tools and safety checks.
- Keep the system prompt and tool list short (small model).
- Update tests in tests/ so pytest passes. After each phase, show a short summary of what changed and how I test it.

PHASE 0 - read only, no edits
Read assistant/brain/gemini_client.py, assistant/brain/agent_loop.py, assistant/tools/registry.py, assistant/interfaces/voice.py, assistant/config.py, main.py. Explain in plain words: how a command flows through, how tools are declared and run, where Gemini is used for transcribe/TTS/live voice, and your plan. Wait for me.

PHASE 1 - local brain (typed commands first)
Create assistant/brain/local_client.py with the same interface as the Gemini client so agent_loop can use either. Convert tool declarations from google.genai types to OpenAI format ({"type":"function","function":{name,description,parameters}}). Send temperature 0.1, top_k 50, repeat_penalty 1.1. Parse tool_calls (retry once on malformed JSON), run the tool, send the result back as a role "tool" message with tool_call_id, and get a short final reply. Max 3 tool rounds, timeouts, clear error if the server is down.

PHASE 2 - auto-start the server
On startup, GET http://127.0.0.1:9931/health. If not ready, launch llama-server as a subprocess (paths in config), log to logs/llama-server.log, wait up to 90 s with a progress message, and stop it when Ultron exits (including Ctrl+C). Never start a second copy. If the port is used by something else, show a clear error.

PHASE 3 - ears
Replace the Gemini transcription calls in interfaces/voice.py with faster-whisper base.en (cpu, int8, beam_size 1, vad_filter on). Load the model once at startup. Reuse the existing mic capture; if there is none, use push-to-talk with a key. Disable Gemini Live voice when BACKEND=local.

PHASE 4 - mouth
Replace Gemini TTS with Piper en_US-lessac-medium. Load the voice once at startup (loading is the slow part). Split replies into sentences and start playing the first one while the rest generate. Use the audio output the project already uses (Fedora uses PipeWire). Let me interrupt it with the key. Spoken replies should be one short sentence: add that to the system prompt.

PHASE 5 - test and measure
Add a script that runs 15 sample commands (open app, volume up/down, etc.) in text mode and prints the chosen tool, its arguments, and timing for each stage (speech-to-text, LLM, first audio). Save timings to logs/. Report which commands failed. EXTRA NOTES FROM THE CODE OUTLINE
- Mirror GeminiAdapter in gemini_client.py: same methods (select_model(role), generate_turn(...)) so AgentLoop.run_task works unchanged. Map every role (main, fast, planner) to the one local model.
- In tools/registry.py keep ToolSpec, the @tool decorator, risk levels, PermissionGate and wrap_untrusted_output exactly as they are. Add get_openai_tools() next to get_gemini_tools(), building OpenAI-format function schemas from the same registered tools.
- Skip the Gemini-only parts when BACKEND=local: google_search tool, thinking levels, UsageMeter daily token cap, model-name validation.
- Keep AgentLoop pause/resume/redirect and dry_run working.
- The 1.7B model is weak at long multi-step plans. Keep tasks to a few tool rounds and keep the PermissionGate confirmations for risky tools.
```

## c) MY APPROVALS

- Phase 1 approved ("Plan approved. Do Phase 1 only, then STOP.").
- Volume tools approved: `wpctl` via subprocess **argument list, no shell**; step clamped **5–20%**; never above 100% (`--limit 1.0`); registered as a **low-risk** tool through the existing PermissionGate. No other new tools.
- Python 3.14 wheel check approved to run after Phase 1 / before Phase 3. Install nothing outside the venv. If a wheel is missing, propose options.
- Fallback idea if a wheel is ever missing: a **separate Python 3.13 venv used only for ears and mouth** (not for the brain).
- Install only into the existing venv using `.venv/bin/python -m pip ...`; nothing with sudo/dnf without approval.
- Phase 1 test conditions: llama-server is started manually (Phase 2 automates it later).

## d) STATUS

### Phase 0 — DONE (read-only)
Files read and explained (flow, tools, Gemini points). Backup created before any edits:
`.backups/ultron-pre-local-20261030-152842.tar.gz` (source, tests, config, requirements, pytest.ini, README, `.env.example`, `.env`).

### Phase 1 — DONE and verified

**Created**
| File | Purpose |
|---|---|
| `assistant/brain/local_client.py` | `LocalAdapter`: OpenAI-compatible client for llama.cpp. Same interface as `GeminiAdapter` (`select_model`, `generate_turn`, `usage_meter`); returns real `google.genai` `GenerateContentResponse` objects so AgentLoop is unchanged. Sends temp 0.1 / top_k 50 / repeat_penalty 1.1 / max_tokens 512; translates genai history ↔ OpenAI messages; parses `tool_calls`; **retries once on malformed JSON** with a corrective `role:"tool"` message; clear errors for server-down/timeout. |
| `assistant/tools/volume.py` | `volume_up` / `volume_down`: `wpctl` (arg list, no shell), step clamped 5–20%, absolute target capped at 1.0 with `--limit 1.0`, `@tool(risk_level="safe", read_only=False)`. |
| `tests/test_local_client.py` | 6 offline tests via `httpx.MockTransport` (text reply, tool parsing, JSON retry once, JSON-twice error, server-down error, history/tool round-trip). |
| `tests/test_volume.py` | 4 tests (clamping, cap at 100/0, no shell, safe registration + OpenAI schema). |
| `tests/test_agent_rounds.py` | Verifies the local **3-tool-round cap** then forces a final no-tools answer. |

**Changed**
| File | Change |
|---|---|
| `assistant/brain/__init__.py` | `build_adapter(config)` factory: local ↔ gemini. |
| `assistant/brain/agent_loop.py` | Backend-aware tool schema (`get_openai_tools` vs `get_gemini_tools`), 3-tool-round cap for local, step label without thinking level locally. Pause/resume/redirect/dry_run unchanged. |
| `assistant/tools/registry.py` | `get_openai_tools()` + `_SCHEMA_TYPE_TO_JSON`; ToolSpec/@tool/risk/gate/wrap untouched. |
| `assistant/config.py` | `backend` (default local), `llama_base_url/server_binary/model_path/timeout`, `whisper_model`, `piper_voice`, `local_model_name`, local model label + switching refusal, `~` expansion, new env prefixes. |
| `assistant/interfaces/cli.py`, `assistant/interfaces/gui.py` | Use `build_adapter()`; register `volume_up`/`volume_down`. |
| `assistant/doctor.py` | `BACKEND=local`: doctor checks llama binary + model; self-test reports backend (no key); `voice-test` prints SKIP. |
| `.env` / `.env.example` | `BACKEND=local`, `LLAMA_*`, `WHISPER_MODEL`, `PIPER_VOICE` (no secrets copied into this doc). |
| `tests/test_phase1.py` | Model switcher test pinned to gemini backend; local behavior asserted; OpenAI schema assertions added. |

**Verification**
- `pytest`: **20 passed**.
- Live against the manual server: `/health 200`; reply turn **1.52 s**; tool choice `list_files({'directory': 'Downloads'})` in **1.55 s**; full CLI loop executed `list_files` and replied.
- `main.py doctor`: PASS for llama-server binary + model file, "Gemini API checks skipped".

**Python 3.14 wheel dry-run (nothing installed yet — all wheels exist)**
`faster-whisper 1.2.1`, `ctranslate2 4.8.4`, `av 19.0.2`, `onnxruntime 1.31.1`, `piper-tts 1.8.0`, plus deps `numpy 2.5.4`, `tokenizers 0.23.3`, `huggingface_hub 2.4.0`, `httpx2 2.16.0`, `httpcore2 2.16.0`. No sudo needed.

**One-time model downloads when Phases 3/4 start**
faster-whisper `base.en` ≈ 75 MB; Piper voice `en_US-lessac-medium` ≈ 65 MB.

### Phase 2 — DONE and verified

**Created**
| File | Purpose |
|---|---|
| `assistant/brain/server_manager.py` | `LlamaServerManager`: `GET /health` first; `ready` → reuse (and never stop) a server someone else started; `503` → wait for the boot in progress; free port → spawn `llama-server` with the exact handoff flags (`cwd` = binary dir, stdout/stderr → `logs/llama-server.log`, `start_new_session`), poll health up to `LLAMA_START_TIMEOUT_SEC` (90 s) with progress lines every 5 s; `occupied`/404/odd HTTP → clear error, **no second copy**. `stop()` terminates only our own child (SIGTERM → 10 s → SIGKILL); `atexit` + SIGTERM/SIGHUP hooks; KeyboardInterrupt during the wait is caught in `main.py`. Also exports `server_status()` (read-only probe) and `port_is_open()`. |
| `tests/test_server_manager.py` | 10 offline tests: URL helpers, reuse, wait-while-loading, spawn flag order, log file, occupied port, foreign HTTP status, timeout stops child, child dies at startup (log tail), single spawn per run, missing binary. |

**Changed**
| File | Change |
|---|---|
| `main.py` | Extracted `_dispatch()`; `chat`/`gui` with `BACKEND=local` call `ensure_running()` first and `close()` in a `finally`. Errors exit 1 with the reason; Ctrl+C at startup exits 130. |
| `assistant/config.py` | `llama_start_timeout_sec` (`LLAMA_START_TIMEOUT_SEC=90`), `llama_server_log` (`LLAMA_LOG_PATH=logs/llama-server.log`). |
| `assistant/doctor.py` | Local branch now reports live server state: running / starting / not running (auto-starts) / port belongs to another program (exit 1). |
| `assistant/brain/local_client.py` | Connection-refused message now points at auto-start and the server log. |
| `.env` / `.env.example` | `LLAMA_START_TIMEOUT_SEC=90`, `LLAMA_LOG_PATH=logs/llama-server.log` (no secrets). |

**Verification**
- `pytest`: **30 passed** (20 existing + 10 new).
- Live reuse: with the manual server up, `ensure_running()` → "reusing it", `started_by_us=False`, and the server was still healthy after `close()` (never killed a server we did not start).
- Live foreign port: `http.server` on 9933 → `LlamaServerError: Port 9933 is used by another program … Ultron will not start a second copy` (spawn stub never called).
- Live spawn/stop with a stand-in binary (port 9934, real Popen): 503 twice → 200, progress `ready in 1.5 s`, log captured, child gone and port closed after `stop()`, second start/stop cycle clean.
- `printf '/quit\n' | .venv/bin/python main.py chat` reuses the running server and leaves it running.

**Not yet exercised for real:** the actual `llama-server` spawn + 25 s model load (the user's manual server holds port 9931). That is step 1–2 of the Phase 2 manual test below.

> Later update: this *was* exercised for real after Phase 3 work — `main.py chat` spawned the real binary and reported `ready in 1.5 s` (warm page cache; mmap makes the load much faster than the 25 s cold estimate), and the port was released on exit. User's manual Phase 2 test: **PASSED**.

### Phase 3 — DONE and verified

**Installed (venv only, no sudo)**
`faster-whisper 1.2.1`, `ctranslate2 4.8.4`, `av 19.0.2`, `onnxruntime 1.31.1`, `numpy 2.5.4`, `tokenizers 0.23.3`, `huggingface-hub 2.4.0`, `hf-xet`, `click`, `protobuf`, `flatbuffers`, `tqdm`, `truststore`, `httpx2`/`httpcore2`.

**One-time model download (already done, lives in the project)**
`models/faster-whisper-base.en/` — `model.bin` (145 MB), `config.json`, `tokenizer.json`, `vocabulary.txt`. Fetched once by `main.py download-models`; everything afterwards is offline (`local_files_only=True`).

**Created**
| File | Purpose |
|---|---|
| `assistant/speech/__init__.py`, `assistant/speech/ears.py` | `WhisperEars`: `base.en`, `device="cpu"`, `compute_type="int8"`, `local_files_only=True`, loaded **once** (thread-safe, idempotent); `transcribe()` uses `language="en"`, `beam_size=1`, `vad_filter=True`, `condition_on_previous_text=False`; joins segments, maps silence/`[BLANK_AUDIO]` to `I didn't catch that`; `load_wav_16k_mono()` decodes WAV with the stdlib `wave` + numpy, downmixes stereo and linearly resamples to 16 kHz; `download_whisper_model()`; clear `EarsError` naming the download command. |
| `tests/test_ears.py` | 11 offline tests: WAV decoding/resampling, missing model message, load-once, CPU/int8/offline settings, decode flags, silence handling, missing file, local controller never touches the cloud, gemini backend keeps cloud path, live voice off locally, download target path. |

**Changed**
| File | Change |
|---|---|
| `assistant/interfaces/voice.py` | `VoiceController.ears` created only when `BACKEND=local`; new `load_ears()`; `transcribe_audio()` dispatches locally and keeps the old Gemini chain as `_transcribe_cloud()`; Gemini TTS is skipped on the local backend (system voice `spd-say` until Phase 4); `adapter` may be `None`. |
| `assistant/interfaces/cli.py`, `gui.py` | Load the ears once at startup and print/append `Ears: ready — faster-whisper base.en ready (CPU int8)`. |
| `assistant/config.py` | `MODELS_DIR`, `whisper_model_dir` (`WHISPER_MODEL_DIR`, default `models/faster-whisper-<model>`), `voice_live_enabled` property (False on local). |
| `assistant/doctor.py` | Local doctor reports the whisper model + "Gemini Live voice: disabled"; `voice-test` on local now really loads the ears, records 3 s and transcribes (STT + timing), TTS stage marked SKIP. |
| `main.py` | New `download-models` command (one-time fetch into `models/`, clear offline error). |
| `.env` / `.env.example` | `WHISPER_MODEL_DIR=models/faster-whisper-base.en`. |
| `tests/test_voice.py` | Cloud fallback test pinned to `BACKEND=gemini` (local now uses the ears). |

**Verification**
- `pytest`: **41 passed**.
- Real model: load **0.34 s**; espeak-generated clips transcribed on CPU in **0.47–0.57 s** each (`"list file in my documents folder."`, `"Turn the volume down by 10%."`, `"What is the weather in Bangalore tomorrow?"`).
- `main.py doctor`: PASS for whisper model, "Gemini Live voice: disabled", llama-server status line.
- `main.py chat` startup: `[llama-server] ready in 1.5 s` (real model, warm page cache) → `Ears: ready` → prompt. Port released on exit.
- Library gotcha recorded: faster-whisper 1.2.1 calls `av.open(..., metadata_errors="ignore")`, which `av` 19.x rejects — so audio is passed to `transcribe()` as a float32 numpy array, which skips `decode_audio` entirely.

### Phase 4 — DONE and verified

**Installed (venv only, no sudo)**
`piper-tts 1.8.0` (+ `pathvalidate 3.3.1`). Wheel is `cp39-abi3`, so it installs and runs on Python 3.14.8.

**One-time model download (already done)**
`models/piper/en_US-lessac-medium.onnx` (63 MB) + `.onnx.json` (4.9 KB), fetched by `main.py download-models` (which now fetches ears + mouth).

**Created**
| File | Purpose |
|---|---|
| `assistant/speech/mouth.py` | `PiperMouth`: loads the voice **once** (`PiperVoice.load(model, config)`, CPU, no CUDA), `synthesize_pcm()` → int16 PCM with 120 ms lead + 200 ms tail silence, `VOICE_VOLUME` gain with a 0.95 peak ceiling and hard clipping protection, WAV written at the voice's real 22 050 Hz; `split_sentences()`; `download_piper_voice()`; `MouthError` naming the download command. |
| `tests/test_mouth.py` | 9 offline tests: sentence split/cap, missing voice message, load-once, WAV format + headroom + lead/tail silence, volume without clipping, empty synthesis, Piper-only local replies (spd-say forbidden), first sentence plays while later ones synthesize, Ctrl+C does not hang. |
| `tests/test_no_gemini.py` | 4 guards: local builds only `LocalAdapter` (with `genai.Client` and `GeminiAdapter` patched to explode), a full local voice turn (STT + speech) never touches Gemini, `voice-test` local never builds a Gemini adapter, voice rule present in the local prompt only. |

**Changed**
| File | Change |
|---|---|
| `assistant/interfaces/voice.py` | `VoiceController.mouth` (local only) + `load_mouth()`; `_speak_local_piper()` streams sentence-by-sentence through a producer thread + queue and plays each WAV with the project's existing `pw-play` chain; local replies cap at 160 chars; `speak_sync()` never reaches Gemini/`spd-say` locally; `_play_wav_file()` now terminates a half-played player so Ctrl+C really stops the audio; `clean_speech_text()` tidies empty slots left by stripped paths (`"like , , , plus"` → `"including directories, plus …"`). |
| `assistant/interfaces/cli.py` | Loads the mouth at startup (`Mouth: ready — Piper en_US-lessac-medium ready (22050 Hz, CPU)`); **Ctrl+C while Ultron is speaking interrupts the speech and keeps the session**; prompt returns. |
| `assistant/interfaces/gui.py` | Loads the mouth at startup and reports it in the transcript; the existing STOP button already stops speech. |
| `assistant/brain/prompts.py` | Local prompt starts with the VOICE rule: one short spoken sentence (<20 words), no file names/paths/numbers. Gemini prompt unchanged. |
| `assistant/doctor.py` | Local doctor checks the Piper voice files; `voice-test` on local now speaks a sample sentence through Piper and reports timing. |
| `main.py` | `download-models` fetches the whisper model **and** the Piper voice. |
| `.env` / `.env.example` | `PIPER_VOICE_DIR=models/piper`. |
| `.gitignore` | `models/` ignored. |

**Verification**
- `pytest`: **54 passed**.
- Real audio: load 1.22 s; synthesis 0.02–0.12 s per sentence; output 22 050 Hz mono 16-bit, peak **31 128** (0.95 full scale → no clipping), lead/tail silence confirmed; `pw-play` reports success.
- Streaming: first audio at **0.03 s** after `speak_sync()`, second sentence played while the first was still talking (natural pipeline, no waiting for all synthesis).
- `voice-test` local (mic stubbed, real whisper + real Piper + real pw-play): all four stages PASS, exit 0.
- Ctrl+C during playback raises, kills `pw-play`, and returns to the prompt (covered by test + `_play_wav_file` cleanup).

**Known limitation to revisit in Phase 5**
The 1.7B model ignores the VOICE rule even when it is rule #1: a tool-heavy turn can take ~20 s to generate a long paragraph, which is then cut to 160 chars for speech. Phase 5 measurement should tune the prompt/tool output size (see also the caching idea in section h).

### Local-mode Gemini audit (requested for Phase 4)

Every `genai` / `GEMINI_` / `GeminiAdapter` reference in the source, and why it cannot call Gemini with `BACKEND=local`:

| Location | What it is | Why it cannot run locally |
|---|---|---|
| `assistant/brain/__init__.py:14` | `return GeminiAdapter(config)` | Only reached when `config.backend != "local"`. |
| `assistant/brain/agent_loop.py:12,14` | `types` import, `GeminiAdapter` type annotation | Type/annotation only; the loop calls `self.adapter`, which is `LocalAdapter`. Tool schemas switch to `get_openai_tools()`. |
| `assistant/brain/local_client.py:17` | `google.genai.types` | Builds local `Content`/`Part`/`FunctionCall` objects for the unchanged AgentLoop; no client, no network. |
| `assistant/interfaces/voice.py:27,29` | `types` import, adapter annotation | `transcribe_audio()` returns the ears result before the cloud path; `speak_sync()` returns the Piper result before the TTS path. |
| `assistant/interfaces/voice.py:602` | "No GEMINI_TTS_MODEL configured" | Inside `synthesize_cloud_tts()`, unreachable while `self.mouth is not None`. |
| `assistant/doctor.py:72-77` | key check + `GeminiAdapter` in `run_doctor` | Local branch returns at the top of the function. |
| `assistant/doctor.py:101-104` | key check + model listing in `run_self_test` | Inside the `else:` of `if config.backend == "local":`. |
| `assistant/doctor.py:177` | `GeminiAdapter(config)` in `run_voice_test` | Local branch returns via `_run_local_voice_test()` (adapter `None`); asserted by `tests/test_no_gemini.py`. |
| `assistant/config.py:165-265` | reads `GEMINI_*` strings, `api_key` field, `has_valid_key` | Pure data loading; nothing is validated and nothing is called. `has_valid_key` is never *evaluated* in local mode. |
| `assistant/safety/redact.py:26` | reads `GEMINI_API_KEY` from env | Only to **mask** it in logs (`redact_secrets`). No validation, no network, and a missing key is fine. Kept on purpose: it is what stops the key leaking into `assistant.log`. |
| `assistant/tools/registry.py:11` | `types` import, `get_gemini_tools()` | Only `get_openai_tools()` is used locally. |
| `assistant/interfaces/cli.py`, `gui.py`, `doctor.py` | `TokenUsageStats` import | Dataclass only. |

**Answer: no. With `BACKEND=local` nothing constructs a Gemini client, checks an API key, or opens a Gemini connection.** The only Gemini-shaped environment read is the redactor masking `GEMINI_API_KEY` out of logs. `tests/test_no_gemini.py` fails loudly if `genai.Client` or `GeminiAdapter` is ever constructed during a local turn.

### Permission mode (default is now `auto-safe`)

`default_mode` lives in `config/settings.yaml` **and** as the `AssistantConfig` fallback (`assistant/config.py`); `PermissionGate` no longer hardcodes `"ask"` — an unset mode follows the config, so CLI, GUI, and every future entry point start in `auto-safe`. Override per run with `main.py --mode ask|auto-safe|human-only` or live with `/mode`.

| Runs without asking (auto-safe) | Still asks (y/N, 60 s timeout, default No) |
|---|---|
| `list_files`, `read_file` (read-only, safe tier) | Any tool labelled `risk_level="confirm"` (delete / move / shell / send tier) |
| `open_app` (launch an app by name only, no arguments) | Any path outside `ALLOWED_ROOTS` (read or write) |
| `volume_up`, `volume_down` (5–20% steps, capped 0–100%) | Any non-read-only call made **after** untrusted tool output looked like prompt injection |
| | Any non-read-only tool whose **name** says it deletes/moves/writes/copies/sends/executes, even if mislabelled `safe` (defence in depth in `PermissionGate.check`) |

Always blocked, never a prompt: `.env` and `.env.*`, `/etc/sudoers`, `/etc/systemd/system`, `/etc/udev/rules.d`, `/etc/polkit-1`, GNOME Keyring / browser password stores, disabling SELinux/firewalld/polkit, editing `assistant/safety/*`, deleting system paths (read-only dirs also confirm on read/write).

`tests/test_permission_modes.py` proves each row above (12 tests), including that a mislabelled `write_file` is denied and the file is never created.

### Phase 5 — PART A DONE (fixes only; the 15-command benchmark is NOT written yet)

**1. Long replies are now one short plain sentence.** `agent_loop._finalize_reply` already
routed local replies through `assistant/brain/reply_style.py`; Phase 5A made it real:

| Change | File |
|---|---|
| Tool results for the local model are compacted before they are sent back: `list_files` → first 15 names + `N item(s)` and **no sizes/paths/timestamps**, `read_file` → first 400 chars, everything else → a small JSON object. Wrapped in the same `<untrusted_tool_output>` fence. The duplicate `data` + full `raw_summary` pair is no longer sent. | `assistant/brain/tool_output.py` (new), `assistant/brain/agent_loop.py` |
| Local generation is capped: **128 tokens** on the first call, **96 tokens** on any follow-up after a tool ran (was 512). | `agent_loop.py`, `local_client.generate_turn(max_output_tokens=…)` |
| Spoken/printed reply cap is a config value: `VOICE_REPLY_MAX_CHARS` (default **160**), used by both the reply shaper and `voice.speak_sync`. | `config.py`, `reply_style.py`, `voice.py`, `.env`, `.env.example` |
| Voice names resolve correctly: "my Downloads folder" / "Downloads" now map to `~/Downloads` instead of `<project>/Downloads`. | `assistant/tools/files.py` |

**2. Prompt is much smaller and cache-friendly.**

| Part | Before | After |
|---|---|---|
| System prompt | 484 chars / **131 tokens** | 289 chars / **84 tokens** |
| Tool schemas | 5 tools / 1831 chars / **485 tokens** | 7 tools / 1517 chars / **405 tokens** (97 → 58 tokens per tool) |
| First call of a task (measured via llama-server usage) | **599** prompt tokens | **455** prompt tokens (−24 %) |
| Call after `list_files` | **5 756** prompt tokens (one 6 921-token turn in the log) | **548** prompt tokens (−90 %) |

How: terse rules, roots collapsed to `~` when home is already allowed, everything volatile
(mode, roots, date) on the trailing `Ctx:` line so the static prefix is byte-identical on every
call; one-line tool docstrings; parameter hints only for **required** params (optional params just
use their default); empty `required`/`description` keys omitted. Note: llama-server b12001 only
caches ~133 tokens of this prompt (`cache_reuse is not supported by this context`), so size is the
lever that works.

**3. Absolute volume + mute.** `set_volume(percent)` clamps 0–100 and calls the same
`wpctl set-volume … --limit 1.0`; `mute_audio(muted=True)` uses `wpctl set-mute`. Both
`risk_level="safe"`, registered in CLI + GUI, and run without asking in `auto-safe`
(proven in `tests/test_volume.py`). "turn volume to 0" now really reaches 0.

**4. Whisper hotwords.** faster-whisper 1.2.1 supports both `initial_prompt` and `hotwords`;
`hotwords` is used because it biases the decoder without injecting fake previous text
(`initial_prompt` conditions the text). The hint is built from the real names in
`config/apps.yaml` plus tool words (open, list files, volume, mute, Downloads…), can be
overridden with `WHISPER_HOTWORDS`, and is passed on every `transcribe()` call.

**5. Microphone clipping.** Every recording is normalised to **70 % of full scale**
(`SAFE_MIC_PEAK = 22 937`): clipped/loud input is turned **down**, quiet speech is boosted at most
**8×**, audio already at the target is untouched. Peak/RMS are logged before and after, kept as
`voice.last_peak_before/last_rms_before` and `last_peak/last_rms`, and printed by
`main.py voice-test` and CLI `/voice`.

**Verification (part A)**
- `pytest`: **89 passed** (was 66; +`test_reply_style.py`, `test_mic_level.py`, `test_file_paths.py`, extra volume/ears tests).
- Live, real model, real wpctl: `turn volume to 30` → `set_volume(percent=30)` → "Volume set to 30%.", `mute` → `mute_audio(muted=True)` → "Audio is muted.", `open firefox` → `open_app('firefox')` → "Firefox is now open.", `list files in my Downloads folder` → `list_files('Downloads')` → "There are 43 items in your Downloads folder." Each turn ≤ 0.02 s of completion + ~1.2 s prompt eval; no lists, no markdown, no sizes.
- `main.py chat` smoke test: reuses the server, runs the turn, clean exit. `main.py doctor`: all PASS, "Gemini API checks skipped".

**Left for part B (not done on purpose)**
- The 15-command timing benchmark.
- Optional speed idea if the user approves: on the *answer* turn (after a tool already ran) drop the
  tool schemas, which costs ~405 of the 548 prompt tokens (~1.1 s per command). Trade-off: no
  second tool call in the same task.

## e) KNOWN COSMETIC ISSUES
- CLI banner still prints `Thinking: [medium]` (ignored locally; step lines already omit it).
- Usage line shows `Today=0/500000 (0.0%)` (no local token cap; harmless).
- `/model <id>` is refused with a clear error under `BACKEND=local` (single fixed model).
- `/voice` transcribes and replies fully locally now (whisper ears → LFM3 → Piper mouth).
- `/thinking` has no effect locally.
- `--dry-run` sends no tools, so LFM3 narrates a tool call as text (expected; same plan-mode idea as before).

## f) RUNNING STATE

- llama-server is started **automatically** by `main.py chat` / `main.py gui` (Phase 2): health check → reuse or spawn → wait ≤90 s → stop on exit. A server started by hand is reused and left running (rollback path for the Phase 1 test). Manual command, if ever needed:

```sh
cd ~/Downloads/LLAMA/llama-b12001-bin-ubuntu-cuda-13.4-x64/llama-b12001
./llama-server -m ~/Downloads/LFM3-1.7B-Q6_K.gguf -ngl 99 --jinja -c 8192 \
  --temp 0.1 --top-k 50 --repeat-penalty 1.1 --reasoning off \
  --host 127.0.0.1 --port 9931
```

- Server output: `logs/llama-server.log` (append mode, safe to delete when no server is running).
- Ears model: `models/faster-whisper-base.en/` (145 MB, downloaded once). Re-download with `.venv/bin/python main.py download-models`; runtime loading is offline-only.
- Mouth model: `models/piper/en_US-lessac-medium.onnx` + `.onnx.json` (63 MB, downloaded once, same command, offline-only).
- Interrupting speech: **Ctrl+C while Ultron talks** stops the audio and keeps the session; Ctrl+C at the prompt exits. GUI: the STOP button.
- Python: always `.venv/bin/python` (3.14.8).
- Pre-change backup: `.backups/ultron-pre-local-20261030-152842.tar.gz`.
- The user's **manual Phase 1 test (4 steps) is still pending**: (1) `.venv/bin/python -m pytest`; (2) interactive `list files in my Documents folder`; (3) `turn the volume down 10` (approve with `y`); (4) `GEMINI_API_KEY= .venv/bin/python main.py` proves no key needed, then `BACKEND=gemini .venv/bin/python main.py doctor` proves rollback. (pytest now prints **89 passed**.)
- **Phase 2 manual test: PASSED** (user-verified).
- **Phase 3 manual test: PASSED** (user-verified).
- **Phase 4 manual test (pending, needs speakers)**: (1) `main.py doctor` → PASS for the Piper voice + `Gemini Live voice: disabled`; (2) `main.py voice-test` → records 3 s, prints what it heard, then *speaks* "Ultron voice test. Local Piper speech is working."; (3) `main.py chat` → `/voice` → ask something short → the reply is spoken by Piper; press **Ctrl+C while it talks** → audio stops immediately and the prompt returns; (4) unplug Wi-Fi and repeat step 2/3 — both voice directions must still work; (5) check `logs/assistant.log` for `Piper … spoke` timing lines.

## g) NEXT STEPS

Rule for every phase: **do ONE phase, STOP, and tell the user how to test it.**

- [x] **Phase 2 — auto-start llama-server**: health check on startup; spawn with exact flags if down; log to `logs/llama-server.log`; wait ≤90 s with progress; stop on exit/Ctrl+C; never a second copy; clear error if the port belongs to something else. *(DONE — user's manual test PASSED)*
- [x] **Phase 3 — ears**: faster-whisper `base.en` (CPU, int8, beam 1, VAD on), loaded once at startup; Gemini transcription replaced in `voice.py`; existing `pw-record` capture reused; Gemini Live voice disabled under local; installed into `.venv` only; model saved in `models/` so it runs offline. *(DONE — see Phase 3 status; mic test pending)*
- [x] **Phase 4 — mouth**: Piper `en_US-lessac-medium`, loaded once at startup; sentence split + stream first sentence while generating the rest; play over PipeWire (`pw-play`); Ctrl+C interrupts speech; "one short spoken sentence" added to the local system prompt. *(DONE — see Phase 4 status; speaker test pending)*
- [ ] **Phase 5 — test & measure**: 15 sample commands in text mode; print chosen tool, args, and per-stage timings (STT / LLM / first audio); save to `logs/`; report failures.
  - [x] **Part A — fixes** (long replies, prompt size, `set_volume`/`mute_audio`, Whisper hotwords, mic normalisation): DONE, see Phase 5 status. `pytest`: 89 passed.
  - [ ] **Part B — 15-command benchmark** (not started by design).

## h) IDEAS

- Voice UX: **press-to-talk first**, wake word later.
- Phase 5 measurement: find out why a full Ultron turn takes ~1.5 s while a bare curl to the server is ~0.55 s. Suspects: system prompt size, the 5-tool schema list, and missing prompt caching on repeated system/tool prefixes.
