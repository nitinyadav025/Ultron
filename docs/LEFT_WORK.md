# LEFT WORK — Phase 9 (Fedora 45 + ULTRON repair), live checkpoint

> Started: 2026-11-06. Branch: `phase9-fedora45-ultron-env` (from `phase8-eyes-hands-ears`
> @ `4d29e58`). Nothing pushed. Stage files by exact filename only.
>
> Machine (verified this session):
> `Fedora release 45 (Forty Five)`, `GNOME Shell 51.1`, `session=wayland`,
> kernel `7.2.15-300.fc45.x86_64`, folder `/home/nitinyadav/Downloads/ULTRON`.

## Gate 1 — Fedora 45 & ULTRON repair + verification (CURRENT)

| Step | Task | Status |
|---|---|---|
| 1 | Clear/init this file, create `phase9-fedora45-ultron-env` | DONE |
| 2 | Fix `.venv` after folder rename + Fedora 45 (system Python 3.14 → 3.15) | IN PROGRESS |
| 3 | Lazy `google.genai` import; remove `"hermes" → "lutris"`; listening cutoff (60 s / 2600 ms / 1800 ms) | PENDING |
| 4 | Fedora 45 + GNOME 51.1 compatibility audit (GPU/llama-server, ears, Piper/PipeWire, AT-SPI, ydotool) | PENDING |
| 5 | `doctor`, full `pytest`, `benchmark` 25/25, `chat`/`listen` smoke | PENDING |
| 6 | Docs + local commit + zero leftover processes | PENDING |
| 7 | STOP, print Nitin's manual verification commands | PENDING |

## Gate 2 — Standalone Living Environment (AFTER Nitin's manual test)

- SQLite memory engine at `~/.local/share/ultron/memory.db`.
- Standalone living environment + Dashboard API server.
- Not started by design; wait for the explicit go-ahead.

---

## Checkpoint 1 — init (2026-11-06)

- Existing left-work material found: `left-work-phase8/` (README, SESSION_STATE,
  ORIGINAL_PROMPT). No `LEFT_WORK.md` existed in root or `docs/`; this file is new.
- `git checkout -b phase9-fedora45-ultron-env` → branch active.
- Baseline docs read: `docs/ULTRON_PROJECT_BIBLE.md`, `docs/PROGRESS.md` (Phase 8 top),
  `docs/ULTRON_LOCAL_HANDOFF.md`, `left-work-phase8/*`.
- Phase 8 end state: 540 passed, benchmark 25/25 @ 1.480 ms (mocked), branch
  `phase8-eyes-hands-ears`, HEAD `4d29e58`, clean tree.

## Checkpoint 2 — venv diagnosis (facts, verified)

`/usr/bin/python3 --version` → **Python 3.15.0** (Fedora 45 upgrade).
`.venv/bin/python --version` → **Python 3.15.0** (it follows `python3 -> /usr/bin/python3`).
`/usr/bin/python3.14` → **does not exist** (only `/usr/bin/python3.15`).
`.venv/pyvenv.cfg` → `version = 3.14.8`, `executable = /usr/bin/python3.14`,
`command = ... venv /home/nitinyadav/Downloads/**assistant-project**/.venv`.
`.venv/lib/` → only `python3.14/` (114 packages, cp314 ABI `.so` files).
`.venv/bin/*` shebangs → `/home/nitinyadav/Downloads/assistant-project/.venv/bin/python3`
(old folder name; does not exist).

Root cause: (a) Fedora 45 removed Python 3.14, so the venv's base interpreter is gone and
`python3 -> /usr/bin/python3` now resolves to 3.15; (b) site-packages live under
`lib/python3.14`, which 3.15 never reads; (c) script shebangs point at the old folder.

Why not just rebuild on 3.15: PyPI has **no cp315 wheel** for `onnxruntime` 1.31.1 or
`av` 19.0.2 (`curl` PyPI JSON: `py315 wheels: NONE`), and Piper/faster-whisper need both.

Fix applied (user-space, no sudo):
1. `uv python install 3.14` → CPython **3.14.9** at `~/.local/bin/python3.14` (uv already
   installed; cpython 3.10/3.11 were pre-existing).
2. Archived the broken venv to `.backups/venv-py314-20261106/` (git-ignored).
3. `/home/nitinyadav/.local/bin/python3.14 -m venv .venv` (new `pyvenv.cfg`:
   `home = /home/nitinyadav/.local/bin`, `version = 3.14.9`).
4. Installed pinned versions matching the last known-good venv:
   `google-genai==2.34.0`, `python-dotenv==1.2.4`, `PyYAML==6.0.3`, `psutil==7.2.2`,
   `send2trash==2.1.0`, `pytest==9.2.0`, `faster-whisper==1.2.1`, `piper-tts==1.8.0`,
   `numpy==2.5.4`, `httpx==0.28.1` (+ resolved deps: ctranslate2 4.8.5, av 19.0.2,
   onnxruntime 1.31.1, tokenizers 0.23.3, huggingface-hub 2.4.0, pydantic 2.14.3).

Verified:
```
.venv/bin/python --version  → Python 3.14.9
.venv/bin/python -c "import faster_whisper, numpy, yaml, requests, pytest" → VENV_OK
.venv/bin/python -c "import ctranslate2, av, onnxruntime, piper, google.genai, httpx, psutil"
  → FULL_OK 4.8.5 1.31.1
.venv/bin/python -m pip check → No broken requirements found.
head -1 .venv/bin/pytest → #!/home/nitinyadav/Downloads/ULTRON/.venv/bin/python
head -1 .venv/bin/pip    → #!/home/nitinyadav/Downloads/ULTRON/.venv/bin/python
```
No hardcoded `assistant-project` reference exists in `assistant/`, `scripts/`, `tests/`,
`config/`, or `main.py` (grep count 0). Remaining hits are historical docs/README text.

Doc/path updates: `AGENTS.md` (Fedora 45, `~/Downloads/ULTRON`, Python 3.14.9 venv rule),
`README.md` (rewritten quick start for Fedora 45 + uv Python 3.14), `requirements.txt`
(added faster-whisper / piper-tts / numpy / httpx + Python 3.14 note),
`docs/PROGRESS.md` actionable "Run from" paths → `~/Downloads/ULTRON`.

## Checkpoint 3 — lazy Gemini, Hermes, listening (done)

### 3.1 Lazy `google.genai`
- New `assistant/genai_types.py`: a lazy `types` proxy; the SDK is imported on first
  attribute access, never at module import.
- `assistant/brain/__init__.py`: `GeminiAdapter`/`LocalAdapter` imported only inside
  `build_adapter()`, based on `config.backend`.
- `assistant/brain/gemini_client.py`: removed top-level `from google import genai` and
  `from google.genai import errors`; `genai.Client` is imported inside the `client`
  property, `errors` inside `generate_turn` before the retry loop.
- `assistant/tools/registry.py`: replaced google-genai declarations with neutral
  `ToolParameter` / `ToolDeclaration` dataclasses built from stdlib `inspect`; `get_gemini_tools()`
  converts them lazily, `get_openai_tools()` never imports google.
- Replaced top-level type imports in `assistant/brain/local_client.py`,
  `assistant/brain/agent_loop.py`, `assistant/interfaces/voice.py` with the lazy proxy.
- Verified: `sys.modules['google']=None; import main; import assistant.brain;
  import assistant.brain.gemini_client` → **LAZY_IMPORT_OK**.

### 3.2 Hermes
- Live scan found a real app: `~/.local/share/applications/com.nousresearch.hermes.desktop`
  and `hermes.desktop`, `Name=Hermes`, `Exec=/home/nitinyadav/.local/bin/hermes desktop
  --skip-build`; binaries `~/.local/bin/hermes`, `hermes-agent`, `hermes-acp`.
- Removed `"hermes": "lutris"` from `APP_ALIASES` (`assistant/tools/apps.py`).
- Removed `hermes` from the Lutris regex in `assistant/speech/wake.py`.
- Added `hermes` to `APP_DISPLAY`, `CLOSE_EXECUTABLES`, and `config/apps.yaml`
  (`desktop_id: com.nousresearch.hermes.desktop`, `binary: hermes`).
- Verified live resolution: `app_key('hermes')='hermes'`, `find_app('hermes')` →
  `Hermes | com.nousresearch.hermes.desktop | /home/nitinyadav/.local/share/applications/com.nousresearch.hermes.desktop`,
  `match_fast_command('open hermes')` → `open_app({"app_name": "hermes"})`.
- Tests updated: `tests/test_phase7_brain_wayland.py`, `tests/test_phase8_eyes_hands_ears.py`.

### 3.3 Listening / conversational replies
- `assistant/config.py`: `voice_max_seconds` 12.0 → **60.0**, `voice_silence_ms`
  1350 → **2600**, `listen_early_check_ms` 850 → **1800** (dataclass + loader).
- `assistant/speech/recorder.py`: `DEFAULT_MAX_SECONDS` 12.0 → **60.0**.
- `assistant/interfaces/listen.py`: detector defaults + `getattr` fallbacks + docstring.
- `assistant/interfaces/voice.py`: fallbacks 2600 / 60.0.
- `.env.example`: `VOICE_SILENCE_MS=2600`, `VOICE_MAX_SECONDS=60`,
  `LISTEN_EARLY_CHECK_MS=1800` + comments. **`.env` does not set these keys** (verified by
  grep), so the new defaults apply; no secret was read or printed.
- Removed the `CHAT_REPLIES` canned-answer map and its routing from
  `assistant/brain/fast_path.py`: greetings / identity / small talk now fall through to
  the local LLM. Action clarifications ("What should I write?") stay deterministic.
- `tests/test_config_defaults.py` updated; `tests/test_b5_full_control.py` now asserts
  conversational turns return `None` (LLM).
- New `tests/test_phase9_fedora45_repair.py` (9 tests).
- Measured: `pytest tests/test_phase9_fedora45_repair.py tests/test_config_defaults.py
  tests/test_b5_full_control.py tests/test_phase7_brain_wayland.py -q` → **92 passed**.

## Checkpoint 4 — Fedora 45 / GNOME 51.1 compatibility audit (live, verified)

| Check | Command / probe | Result |
|---|---|---|
| OS | `cat /etc/fedora-release` | **Fedora release 45 (Forty Five)** |
| GNOME | `gnome-shell --version` | **GNOME Shell 51.1** |
| Session | `$XDG_SESSION_TYPE $WAYLAND_DISPLAY` | **wayland wayland-0** |
| Kernel | `uname -r` | `7.2.15-300.fc45.x86_64` |
| GPU | `nvidia-smi` | GTX 1650, 4096 MiB, driver 615.78.08, idle 4 MiB before test |
| llama-server start | `LlamaServerManager.ensure_running()` | **ready in 2.5 s**, health 200 |
| llama completion | `POST /v1/chat/completions` (`id_slot:0`, `cache_prompt:true`) | call 1 **0.389 s**, call 2 **0.289 s**, reply `ULTRON_FEDORA45_OK`, 38 prompt / 9 completion tokens |
| GPU offload | `nvidia-smi --query-compute-apps` while running | **llama-server, 1606 MiB** on GPU; GPU 1612/4096 MiB, 16 % |
| Server cleanup | `mgr.close()`; `ss -ltnp \| grep 9931`; `pgrep -a llama-server` | port free, no process |
| Ears | `WhisperEars.load()` + 1 s float32 array | small.en load **1.41 s**, transcribe **0.10 s**, `ok=False` → “I didn't hear anything.” (correct for silence) |
| Mouth | `PiperMouth.load()` + `synthesize_pcm()` | en_US-lessac-medium load **1.79 s**, synth **0.18 s**, **122 144 bytes @ 22 050 Hz** |
| PipeWire | `pw-record --version`, `pw-play --version`, `wpctl status` | tools present; PipeWire 1.6.11 running |
| GNOME introspect key | `gsettings get/set org.gnome.shell introspect` | **No such key “introspect”** (GNOME 51.1 still lacks it) |
| GetWindows | `gdbus … org.gnome.Shell.Introspect.GetWindows` | **AccessDenied: GetWindows is not allowed** |
| toolkit-accessibility | `gsettings set/get org.gnome.desktop.interface toolkit-accessibility` | **true** |
| AT-SPI runtime | `/usr/bin/python3` import `gi`/`Atspi`/`pyatspi` | **Atspi OK**, **pyatspi OK** |
| AT-SPI helper | `{"cmd":"ping"}` + `{"cmd":"snapshot"}` on real ptyxis window | ping OK; **75 nodes / 11 elements**, real bounds, actions (`@e1` New Terminal … `@e11` Close Tab) |
| Screen context | `ensure_accessibility()` + `refresh_screen_context()` | `{'toolkit_accessibility': True, 'introspect': False}`; windows `['Brave','Google Chrome','Visual Studio Code']`, `focused_window='Brave'` (wmctrl/process fallback — honest) |
| `/dev/uinput` | `ls -l`, `getfacl` | `crw-rw----+`, ACL **user:nitinyadav:rw-** |
| wl-clipboard | `wl-copy` + `wl-paste` roundtrip | **wl-clipboard 2.3.0**, roundtrip exact |
| ydotoold | `timeout 2 ydotoold --socket-path=/tmp/.ultron_test_ydotool.sock` | socket created as this user; process gone after timeout; **warning `unable to find device pointer:ydotoold virtual device` on GNOME 51.1** (XWayland xinput cannot see the uinput device; live key injection remains UNVERIFIED) |
| Leftover daemons | `pgrep -a ydotoold` | none |

GNOME 51.1 behaves exactly like GNOME 50.5 for introspection: the key is gone and
`GetWindows` is denied, so the Phase 8 AT-SPI path remains the real eye. No code change was
needed for that; `ensure_accessibility()` already reports `introspect: false`.

## Checkpoint 5 — doctor, full suite, benchmark, smoke (verified)

| Check | Command | Result |
|---|---|---|
| Doctor | `.venv/bin/python main.py doctor` | **all PASS**; `Backend=local`, llama binary/model, small.en, Piper; exit 0 |
| Full suite | `.venv/bin/python -m pytest -q` | **547 passed, 0 failed, 0 errors** (76.54 s; first run after the fixes was 544/547 with 3 expected regressions, all fixed) |
| Benchmark | `.venv/bin/python main.py benchmark` | **25/25 passed**, average **2.802 ms** (mocked) |
| Chat smoke | `printf 'hello\n/quit\n' \| .venv/bin/python main.py chat` | server ready 1.5 s; banner `Ultron ready on Fedora Linux 45.`; real LLM reply `Hey there!`; exit 0; port free afterwards |
| Listen help | `.venv/bin/python main.py listen --help` | exit 0, full usage printed |
| Compile | `.venv/bin/python -m compileall -q assistant main.py scripts tests/test_phase9_fedora45_repair.py` | **COMPILE_OK** |
| Whitespace | `git diff --check` | **DIFF_CHECK_OK** |

Three test regressions surfaced and were fixed honestly:
1. `tests/test_listen_reader.py` — the reader test now pins `voice_silence_ms = 1350` (it
   tests buffering, not the new 2.6 s paragraph window).
2. `tests/test_no_gemini.py` — patches `assistant.brain.gemini_client.GeminiAdapter`
   (the adapter is lazy now, so it no longer exists on `assistant.brain` at module scope).
3. `scripts/benchmark_phase5c.py` — the `hello` case now runs `mode="chat"` through the real
   AgentLoop and expects a one-sentence natural reply (`short_plain_reply` keeps only the
   first sentence).

Cosmetic Fedora 45 fix: `assistant/os_release_name()` (stdlib `platform.freedesktop_os_release()`)
replaces the hardcoded “Fedora 44” in the CLI banner and GUI title; `main.py --help` now says
“Ultron — Fedora local voice assistant”. Verified live: `Ultron ready on Fedora Linux 45.`

## Checkpoint 6 — commit and zero leftovers

- Gate 1 commit: **`0bf9d43`** — “P9: Fedora 45 repair — Python 3.14 venv, lazy
  google-genai, Hermes, listening” (33 files, +791/−155) on
  `phase9-fedora45-ultron-env`.
- Staged by exact filename; **`.env` never staged**; `models/`, `workspace/`, `logs/`,
  `.backups/`, `.venv/` untouched.
- Leftover processes checked after the live tests:
  `ss -ltnp | grep 9931` → **empty**; `pgrep -a llama-server` → **none**;
  `pgrep -a ydotoold` → **none**; `pgrep -af atspi_helper.py` → **none**;
  `pgrep -a agent-desktop` → **none**.
- `git status --short` → **clean** after the commit.
- Nothing pushed; `origin` untouched.

## Step 7 — STOP: Nitin's manual Fedora 45 verification

Run these from `/home/nitinyadav/Downloads/ULTRON`. The coding agent cannot use the
microphone, speakers, or inject real keys/clicks.

```sh
cd /home/nitinyadav/Downloads/ULTRON
git branch --show-current        # expect: phase9-fedora45-ultron-env
git log --oneline -2             # expect: P9 repair commit (0bf9d43)
.venv/bin/python --version       # expect: Python 3.14.9

# 1. Static checks
.venv/bin/python main.py doctor
.venv/bin/python -m pytest -q    # expect: 547 passed
.venv/bin/python main.py benchmark   # expect: 25/25

# 2. Typed chat (real local LLM, no canned strings)
.venv/bin/python main.py chat
#   type: hello                  -> a natural model answer (live run gave "Hey there!")
#   type: what is your name      -> a natural model answer, not a fixed string
#   type: /quit

# 3. Voice: the long-paragraph cutoff test
.venv/bin/python main.py chat
#   type: /voice
#   speak one long paragraph (e.g. GTA V and your friends) WITH natural 1-2 s pauses;
#   it must not stop mid-paragraph. Then ask a short question and confirm a natural answer.
#   type: /quit

# 4. Hands-free, Hermes and listening
.venv/bin/python main.py listen
#   say: "Ultron, open hermes"        -> Hermes opens, NEVER Lutris
#   say: "Ultron, open lutris"        -> Lutris still resolves correctly
#   say: "Ultron, what is on my screen" -> active window/site + visible controls
#   speak a longer paragraph again     -> no mid-sentence cutoff
#   say: "Ultron is stop listening"

# 5. Cleanup
ss -ltnp | grep 9931 || echo "no llama-server"
pgrep -a llama-server || echo "no llama-server process"
pgrep -a ydotoold || echo "no ydotoold"
pgrep -af atspi_helper.py | grep -v pgrep || echo "no AT-SPI helper"
```

Notes for the live pass:

- If an old shell exports `WHISPER_MODEL=base.en`, prefix live commands with
  `env -u WHISPER_MODEL -u WHISPER_MODEL_DIR`; the current session exports small.en.
- The first `listen`/`chat` starts llama-server automatically (about 1.5–2.5 s warm).
- `ydotoold` may print `unable to find device pointer:ydotoold virtual device` on
  GNOME 51.1. Real key/mouse injection is still **UNVERIFIED**; report whether clicks and
  typing work.
- Live clicks, typing, the browser `Ctrl+F` tier, and OCR remain **UNVERIFIED**.
- If anything fails, keep the tail of `logs/assistant.log` plus the newest
  `workspace/voice_debug/attempt_*` and report the exact `Fast path` / `Tool call` /
  `Ears` lines.

**Do NOT start Gate 2** (Standalone Living Environment, `~/.local/share/ultron/memory.db`,
Dashboard API) until Nitin reports this live pass. The full blueprint is in
`docs/PROGRESS.md`.
