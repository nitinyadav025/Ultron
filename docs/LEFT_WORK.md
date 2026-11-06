# LEFT WORK — Gate 2A (Standalone Environment) + Gate 2B (Dashboard handoff)

> Started: 2026-11-06. Branch: `phase9-fedora45-ultron-env` (Gate 1 commits `0bf9d43`,
> `e2c74db`). Nothing pushed. Stage files by exact filename only.
>
> Machine: Fedora Linux 45, GNOME 51.1, Wayland, Python 3.14.9 in `.venv`, GTX 1650 4 GB.
> Gate 1 state: **547 tests passed**, benchmark **25/25**, doctor all PASS.

## Gate 2A — Ultron standalone living environment (CURRENT)

| Part | Task | Status |
|---|---|---|
| 1 | Clear this file; build `assistant/environment/` with thread-safe `EnvironmentState` | DONE |
| 2 | Real-time voice controller, barge-in `mouth.stop_speaking()`, true LLM brain replies, 16-turn history | DONE |
| 3 | Brave/Firefox browser bridge extension + `browser_tabs` tools with AT-SPI/ydotool fallback | DONE |
| 4 | HTTP + SSE environment server on `127.0.0.1:9932` (REST, CORS, config, apps, tabs, skills) | DONE |
| 5 | Standalone launcher, `main.py environment`, App Grid `.desktop` icon | DONE |
| 6 | `docs/DASHBOARD_API_CONTRACT.md` for the Gate 2B UI agent | DONE |
| 7 | Full pytest + benchmark + doctor + live `--headless` smoke + commit + process cleanup | DONE |

## Gate 2B — Liquid Glass V18 Dashboard handoff (AFTER Gate 2A)

- [ ] UI specialist reads `docs/DASHBOARD_API_CONTRACT.md` and `assistant/environment/server.py`.
- [ ] Builds the Apple visionOS Liquid Glass V18 dashboard in `assistant/dashboard/dist/`
      (served by the environment server at `http://127.0.0.1:9932/`).
- [ ] Uses `GET /api/state` + `GET /api/stream` for live state, and the documented REST
      actions for voice/config/apps/tabs/skills.
- [ ] Verify from the same repo against the running environment server; no new Python deps.
- [ ] Gate 2B is NOT started by this agent; Gate 2A must finish and pass first.

---

## Checkpoint 1 — environment state (2026-11-06)

### Files
- `assistant/environment/__init__.py` — exports `EnvironmentState`, `environment_state`,
  `get_state`.
- `assistant/environment/state.py` — thread-safe singleton, stdlib only.

### State contract (implemented)
- `status` (`starting|online|listening|transcribing|thinking|speaking|muted|error`).
- `voice_mode` (`wake_word|continuous|ptt`, default `wake_word`).
- `mic_muted`, `tts_muted`, `audio_level_rms`, `audio_peak`.
- `current_transcript`, `current_thought`, `current_action`
  (`{tool, args, status, timestamp}`), `next_planned_steps`, `last_reply`.
- `conversation_feed`: deque(maxlen=50), each turn
  `{id, timestamp, role, text, tools_called, latency_ms, source}`.
- `screen_state`: `focused_window`, `focused_app`, `active_site`, `open_windows`,
  `visible_elements`, `browser_tabs`; plus top-level `browser_tabs`.
- `telemetry`: `gpu_vram_used_mb`, `gpu_vram_total_mb` (4096), `gpu_util_pct`, `cpu_pct`,
  `ram_used_mb`, `llama_server_alive`, `p50_ms`, `p95_ms`, `latency_series`
  (deque maxlen=30, `{ts, transcribe_ms, llm_ms, tool_ms, total_ms}`).
- `live_config` persisted atomically to `workspace/ultron_environment_config.json`:
  `assistant_name=Ultron`, `user_name=Nitin`, `personality_prompt=""`,
  `reply_style=natural`, `brain_replies=True`, `voice_silence_ms=2600`,
  `voice_max_seconds=60.0`, `listen_early_check_ms=1800`, `listen_followup_s=8.0`,
  `mic_source_volume=0.21`, `search_engine=google`, `permission_mode=auto-safe`.
- Change notification: monotonic `revision` + `Condition`; SSE can wake immediately via
  `wait_for_change(last_revision, timeout)`.
- `snapshot()` deep-copies every field for the HTTP API.

### Verification
```
.venv/bin/python - <<'PY' ... EnvironmentState ... PY
→ STATE_OK 12 3000
```
The smoke test covered status/mode/transcript/thought/action/turn/latency/screen updates,
config persistence (`assistant_name=Jarvis`, `voice_silence_ms=3000`), and rejection of an
unknown config key.

## Checkpoint 2 — voice, barge-in and true LLM replies (2026-11-06)

### Files
- `assistant/speech/mouth.py` — adds `_active_player`, `adopt_player`, `release_player`,
  `play_wav_bytes`, `speak`, `is_speaking`, and `stop_speaking()`.
- `assistant/interfaces/voice.py` — `_play_wav_file` adopts/releases its `pw-play` process
  with the mouth; `VoiceController.stop_speaking()` also calls `mouth.stop_speaking()`.
- `assistant/environment/voice_controller.py` — new managed daemon thread.
- `assistant/brain/agent_loop.py` — live thought/action hooks, true-LLM final replies,
  llama-server fallback, 16-turn / 32-message history.
- `assistant/brain/prompts.py` — persona/user/reply-style support (empty defaults keep the
  old byte-identical static prompt).
- Tests pinned to the deterministic mode where they test code replies:
  `tests/test_agent_rounds.py` (autouse `brain_replies=False` fixture, pure-chat prompt
  bound 360), `tests/test_phase7_brain_wayland.py` (write test pins False).

### Barge-in
`PiperMouth.stop_speaking()` sets a stop event, `terminate()`s the tracked player, waits
20 ms, then `kill()`s if needed, and publishes `status=online`, RMS/peak 0. `speak()`
checks the stop event between sentences. `play_wav_bytes()` publishes `speaking` and the
synthesized sentence envelope (RMS/peak) to the shared state.

### Voice modes
- `wake_word`: prefix `wake_match`; `wake_match_anywhere` only when it yields a non-empty
  command; wake-only says "Yes?" and opens `listen_followup_s` (8.0 s).
- `continuous`: every reliable utterance; only a wake spelling at the *start* is stripped
  (`_without_leading_wake`), so "hello Ultron" is not emptied by a fuzzy 2-word match.
- `ptt`: `start_ptt()` buffers raw frames, `stop_ptt()` transcribes immediately; the
  detector/silence timeout is not used.
- Live mic levels are published every 2 frames (40 ms); muted frames are skipped.
- Empty/noise/guard-rejected transcripts are ignored via `has_real_word` +
  `WhisperEars.last_confidence["verdict"]` (the same `transcript_is_unreliable` guard).

### True LLM replies
- `AgentLoop._brain_replies_enabled()` reads `EnvironmentState.live_config`.
- When True, the deterministic early return and `_finalize_reply` code override are
  skipped, so the LLM answers from the real tool result. If `generate_turn` raises
  (llama-server unreachable), the loop falls back to `tool_replies.py`.
- History cap: `HISTORY_MAX_MESSAGES=32`, `HISTORY_MAX_TURNS=16`; pure chat sends the full
  capped history; tool JSON is still pruned to text.
- Thought/action updates: `Thinking about: …`, `Running <tool>…`, `Finished <tool>.`,
  `current_action={tool,args,status,timestamp}`.

### Verification
```
pytest tests/test_agent_rounds.py tests/test_phase7_brain_wayland.py \
       tests/test_phase9_fedora45_repair.py tests/test_mouth.py tests/test_no_gemini.py
→ 93 passed
```
Voice smoke (fake ears/mouth/brain, temp state): continuous "hello Ultron" → brain called
with the full phrase, assistant turn + latency stored, mouth said the reply; wake mode
ignored "random chatter"; `_without_leading_wake("Ultron open firefox")` → "open firefox".

## Checkpoint 3 — browser bridge (2026-11-06)

### Files
- `assistant/environment/browser_bridge.py` — `BrowserBridgeHub`:
  thread-safe tab registry + DOM cache + command queue; `sync`, `poll`, `list_tabs`,
  `find_tab`, `read_tab`, `switch_tab`, `interact`, `record_result`, `connected`.
  Tab discovery merges extension data + CDP `127.0.0.1:9222/json` + `wmctrl -l`.
- `assistant/tools/browser_tabs.py` — three registered tools:
  - `list_browser_tabs()` → `{ok, count, tabs, bridge_connected}`.
  - `switch_to_tab(query)` → extension switch, else `wmctrl -a`, else AT-SPI
    `click_element(query)`.
  - `interact_with_tab(tab_query, action, target="", text="")` → read / click / type /
    send through the extension; when the bridge is absent it falls back to
    `read_screen`, `click_element`, `focus_and_type` + `press_key("enter")`.
- `extensions/ultron-browser-bridge/`:
  - `manifest.json` (MV3 for Brave/Chrome), `manifest.firefox.json` (MV2 for Firefox
    `about:debugging`), `background.js`, `content.js`, `README.md`.
  - `background.js` polls `POST /api/browser/sync` every 800 ms, sends all http(s) tabs and
    the active DOM snapshot, executes `switch_tab`, `close_tab`, `open_url`, `click_dom`,
    `type_dom`, `read_dom`, `scroll_dom`, and returns results on the next sync.
  - `content.js` assigns `@b1…@bN` refs to visible interactive elements, extracts
    WhatsApp (chat list/unread/latest messages), Gemini/Claude/ChatGPT (New chat, prompt,
    Send, latest answer), YouTube (search, video cards, Show more, play/pause) and Gmail
    (unread rows, Compose); handles clicks and React/contenteditable typing via
    `execCommand('insertText')` + `InputEvent`, then Enter/Send.
  - Registered `list_browser_tabs`, `switch_to_tab`, `interact_with_tab` in the CLI, GUI
    and hands-free registries.
- Extension loading is manual (Brave `brave://extensions` → Developer mode → Load
  unpacked; Firefox `about:debugging` → Load Temporary Add-on → `manifest.firefox.json`).

### Verification
```
node --check background.js && node --check content.js     → JS_SYNTAX_OK
json.load(manifest.json / manifest.firefox.json)          → MANIFEST_OK
Python hub smoke: sync 2 tabs + WhatsApp DOM, find/read/switch/click, poll actions
→ actions: [('switch_tab', 12, ''), ('click_dom', 12, 'New chat')] ; BRIDGE_OK
```
Live extension-to-server sync is verified in Part 7 against the running environment
server; actual DOM clicks are still hardware/browser-dependent (UNVERIFIED until Nitin
loads the extension).

## Checkpoint 4 — environment HTTP/SSE server (2026-11-06)

### File
- `assistant/environment/server.py` — stdlib `ThreadingHTTPServer` on
  `127.0.0.1:9932` (`ULTRON_ENV_PORT` override). CORS `*`, JSON errors, 1 MB body cap.
- Routes: `/api/health`, `/api/state`, `/api/stream` (SSE), `/api/command`,
  `/api/voice/control`, `GET/POST /api/config`, `/api/apps`, `/api/apps/action`,
  `/api/tabs`, `/api/tabs/action`, `POST /api/browser/sync`, `GET /api/browser/poll`,
  `POST /api/browser/result`, `GET/POST/DELETE /api/skills`, `/` + static dashboard.
- `assistant/brain/skills.py` — added `delete_user_fact`, `delete_user_alias`,
  `delete_user_skill` for the skills API.
- Static serving prefers `assistant/dashboard/dist/` then `assistant/dashboard/static/`;
  until Gate 2B builds them, `/` returns a 200 readiness page.

### Verification (ephemeral port smoke)
```
EnvironmentServer(port=0).start() → http://127.0.0.1:44103
GET /api/health  → 200 {"ok":true,"version":"phase9-env",...}
GET /api/state   → 200 full snapshot
POST /api/command {"text":"hello","speak":false}
                 → 200 {"reply":"Echo: hello","tools_called":["demo"]}
POST /api/voice/control {"action":"barge_in","mode":"continuous"} → 200
POST /api/config {"assistant_name":"Jarvis"} → persisted in RAM/temp file
POST /api/browser/sync → tab_count 1
GET /api/skills → ok
GET /api/stream → `retry: 1000`, `id: 14`, `event: state`, `data: {...}`
SERVER_OK
```
Fixed during the live run: `port_is_open()` takes `(host, port)`, so health/telemetry now
report the real llama-server state instead of always `false`.

## Checkpoint 5 — standalone launcher (2026-11-06)

### Files
- `assistant/environment/launcher.py` — `EnvironmentLauncher` + `TelemetryCollector` +
  `run_environment()`.
  Start order: registry (28 tools incl. browser tabs/skills) → permissions/adapter/AgentLoop
  → llama-server → ydotool → accessibility → ScreenWatcher → PiperMouth/WhisperEars →
  EnvironmentVoiceController → telemetry (2 s) → HTTP server → chromeless Dashboard window.
  Stop: voice → screen watcher → ydotool → telemetry → server → llama-server → dashboard
  window; state returns to `online`.
- `main.py` — `environment` / `app` / `start` commands, `--headless`, `--port`.
- `scripts/ultron-launch.sh` (executable) — `exec .venv/bin/python main.py environment`.
- `~/.local/share/applications/ultron.desktop` — installed, `Terminal=false`,
  `Exec=/home/nitinyadav/Downloads/ULTRON/scripts/ultron-launch.sh`.

### Live headless smoke (actually run)
```
.venv/bin/python main.py environment --headless --port 19932
GET /api/health → {"ok":true,"version":"phase9-env","voice_status":"listening"}
GET /api/state  → voice_mode=wake_word, GPU 1612/4096 MiB, CPU 11 %, RAM 9067 MB
POST /api/command {"text":"what is your name?","speak":false}
 → {"reply":"I'm Ultron, your calm and helpful Fedora GNOME voice assistant.",
    "tools_called":[],"latency_ms":1739.69}
SIGTERM → clean stop; no environment process, no llama-server, no ydotoold,
          no pw-record, no AT-SPI helper, ports 19932/9931 free.
```
The real LLM path is confirmed through the API (1.74 s first reply on this machine).
`--headless` still starts the voice controller; the smoke briefly opens `pw-record` and
closes it on SIGTERM. Dashboard windows are only opened without `--headless`.

## Checkpoint 6 — Dashboard API contract (2026-11-06)

- `docs/DASHBOARD_API_CONTRACT.md` written for the Gate 2B UI agent. It documents:
  - frontend location (`assistant/dashboard/dist/`, fallback `static/`) and relative-asset
    rule;
  - start/preview commands (`main.py environment`, `--headless`, `--port`);
  - exact JSON for `GET /api/health`, `GET /api/state`, `GET /api/stream` (SSE
    `event: state`, `id: <revision>`), `POST /api/command`, `POST /api/voice/control`,
    `GET/POST /api/config` (all keys/defaults/ranges), `GET /api/apps` +
    `POST /api/apps/action`, `GET /api/tabs` + `POST /api/tabs/action`,
    `/api/browser/sync` + `/api/browser/poll`, `GET/POST/DELETE /api/skills`;
  - CORS, error format, 50-turn feed, 30-point latency ring, 250 ms SSE cadence;
  - the 1:1 mapping from UI controls (Stop → `barge_in`, mode switch, mute mic/TTS) to
    API calls, and a warning not to add CDNs if offline purity matters.

## Checkpoint 7 — verification, live smoke, commit (2026-11-06)

### Automated
- New `tests/test_phase9_environment.py` → **19 passed**. Covers:
  state defaults/ring buffers/config persistence/change wake-up; live HTTP
  health/state/SSE/command/voice modes/barge-in/config/apps/app action/browser sync/tabs
  action/skills round-trip; browser tools list/switch/click/read; `mouth.stop_speaking()`
  termination; voice continuous/PTT/wake+follow-up; true-LLM confirmation and
  unreachable-brain fallback; 16-turn history via the updated Phase 6 test.
- Full suite: **566 passed, 0 failed, 0 errors** (67.02 s).
- Benchmark: **25/25 passed**, average **2.008 ms** (mocked).
- `main.py doctor`: all PASS (local backend, small.en, Piper, llama binary/model).
- `compileall` OK; `git diff --check` clean.

### Live smoke on the real desktop (port 9932, actually run)
```
.venv/bin/python main.py environment --headless
GET /api/health → {"ok":true,"version":"phase9-env","llama_server":true,"voice_status":"listening"}
GET /api/state  → status=listening, voice_mode=wake_word
                  focused_window="nitinyadav@Sasuke:~/Downloads/ULTRON",
                  focused_app="terminal", open_windows=[that window],
                  visible_elements=11,
                  telemetry={gpu_vram_used_mb:1612, gpu_vram_total_mb:4096,
                             gpu_util_pct:0.0, cpu_pct:6.0, ram_used_mb:8946,
                             llama_server_alive:true, ...}
GET /api/stream → retry: 1000; id: 371; event: state; data: {...}
POST /api/command {"text":"what is your name?","speak":false}
 → {"ok":true,"reply":"I'm Ultron, your calm and helpful Fedora GNOME voice
    assistant.","tools_called":[],"latency_ms":1419.44}
POST /api/browser/sync (WhatsApp tab + DOM) → {"ok":true,"tab_count":1}
GET /api/tabs → count=1, title=WhatsApp, bridge_connected=true
GET / → 200
SIGTERM → clean stop
```
Cleanup verified after shutdown: no environment process, no `llama-server`, no `ydotoold`,
no `pw-record`, no `atspi_helper.py`; ports **9932** and **9931** free.

### Commit
- Code/docs commit hash: **filled in the follow-up docs commit** (this checkpoint is
  committed after the code commit). Branch `phase9-fedora45-ultron-env`, local only.
- Staged by exact filename; `.env`, `models/`, `workspace/`, `logs/`, `.backups/`, `.venv/`
  never staged. Nothing pushed.

## Gate 2B — handoff for the Liquid Glass V18 Dashboard agent

1. Read `docs/DASHBOARD_API_CONTRACT.md` and `assistant/environment/server.py`.
2. Build the frontend in `assistant/dashboard/dist/` (single-page static bundle; relative
   assets). Start with `.venv/bin/python main.py environment`.
3. Use `GET /api/state` once, then `EventSource("/api/stream")`; drive every control through
   the documented REST actions.
4. Do not modify the backend unless the contract truly needs it; if so, update the contract,
   tests and `docs/LEFT_WORK.md` together.
5. Verify live at `http://127.0.0.1:9932/`, including voice mode switch, Stop/barge-in,
   memory/skills edits, app launch/focus and browser tabs.

### Nitin's manual check (also after Gate 2B)

```sh
cd ~/Downloads/ULTRON
.venv/bin/python main.py environment        # opens a dedicated Dashboard window
# in another terminal:
curl -s http://127.0.0.1:9932/api/health
curl -s http://127.0.0.1:9932/api/state | head -c 600
# Load the extension once:
#   Brave: brave://extensions → Developer mode → Load unpacked →
#          extensions/ultron-browser-bridge/
#   Firefox: about:debugging → Load Temporary Add-on → manifest.firefox.json
# Then ask: "Ultron, what tabs are open?" / "switch to WhatsApp" /
#           "check what messages came in on WhatsApp" / "open a new chat in Gemini"
# Stop with Ctrl+C, then:
ss -ltnp | grep -E "9931|9932" || echo "ports free"
pgrep -a llama-server || echo "no llama-server"
pgrep -a ydotoold || echo "no ydotoold"
pgrep -a pw-record || echo "no pw-record"
pgrep -af atspi_helper.py | grep -v pgrep || echo "no helper"
```

**UNVERIFIED by this agent:** the real microphone voice loop, the extension's actual DOM
click/type in Brave/Firefox, and hardware barge-in audio. The code paths are unit-tested and
the local server was live-smoked; those three need Nitin's desktop.
