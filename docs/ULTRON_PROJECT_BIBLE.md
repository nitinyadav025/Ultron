# Ultron — The Complete Project Bible

> **What this file is.** One long-lived, detailed document for every human or AI agent who
> touches this project. It records the idea, the architecture, every phase, every live
> failure, the root cause we found, the fix, the tests that pin it, and the ideas still on
> the table. `docs/PROGRESS.md` remains the chronological engineering log; this file is the
> single entry point that makes a fresh session productive in minutes.
>
> **Rules for future agents:** read this file, then `docs/PROGRESS.md`, then
> `docs/ULTRON_LOCAL_HANDOFF.md`, then `AGENTS.md`. Never guess: verify by reading files or
> running commands. Mark anything not actually run as **UNVERIFIED**. Keep the local-first
> guarantee (`BACKEND=local` never calls Gemini). Never commit `.env`, `models/`,
> `workspace/`, `logs/`, `.backups/` or `.venv/`. Never push without explicit approval.

---

## 0. Snapshot

| Item | Value |
|---|---|
| Project | Ultron, a voice-controlled Fedora 44 GNOME Wayland laptop assistant |
| Folder (verified) | `/home/nitinyadav/Downloads/ULTRON` (older text says `~/Downloads/assistant-project`; that path does not exist on this machine) |
| Python | `.venv/bin/python` (3.14.8) — never the system `python3` |
| Brain | LFM3-1.7B GGUF on llama.cpp `llama-server`, OpenAI-compatible API `http://127.0.0.1:9931/v1` |
| Ears | faster-whisper `small.en`, CPU, int8, beam 2, float32 numpy arrays only (automatic `base.en` fallback) |
| Mouth | Piper `en_US-lessac-medium`, CPU, streaming sentence by sentence |
| Eyes | AT-SPI2 helper under `/usr/bin/python3`, optional `agent-desktop@1.2.0`, OCR fallback (`tesseract`, not installed) |
| Hands | AT-SPI actions, `ydotool` absolute coordinates, browser `Ctrl+F` tier, `wl-copy` paste |
| GPU | GTX 1650 4 GB, reserved for the LLM only |
| Default backend | `BACKEND=local` (no API key needed) |
| Rollback | `BACKEND=gemini` must keep working |
| Branch (Phase 8) | `phase8-eyes-hands-ears` (created from `phase7-llm-brain-wayland` @ `242a57f`) |
| Test state at Phase 7 end | **503 passed, 0 failed, 0 errors** (`pytest -q`, 38.82 s) |
| Test state at Phase 8 end | **540 passed, 0 failed, 0 errors** (`pytest -q`, 53.50 s) |
| Benchmark at Phase 8 end | **25/25 passed**, average **1.480 ms** (mocked; 1.516 ms on the Part 7 re-run, 2.109 ms on the first run) |
| Remote | Nothing is pushed. The user pushes when they choose. |

---

## 1. The idea

Ultron is not a chatbot in a terminal. It is a **local, always-available laptop operator**:
the user speaks, the laptop hears, a small local LLM chooses a real tool, the tool actually
runs on GNOME/Wayland, and Ultron answers in one natural spoken sentence. The target
experience is Jarvis-like competence with a hard safety boundary: the assistant may open
and close apps, search the web, type, scroll, control tabs and windows, change volume or
brightness, read allowed folders, and report system status — but never read secrets, never
run sudo, never disable security, and never invent an action that did not happen.

Design instincts that survived every phase:

1. **Local first.** Speech-to-text, reasoning, and speech all run on this laptop. No API key
   is needed for normal use.
2. **Real actions, honest replies.** A reply is built from the actual tool result whenever
   code already knows the answer. The 1.7B model is not trusted to invent counts, paths, or
   success.
3. **Fast path for known sentences, LLM for everything else.** Deterministic routing handles
   the common commands; the model handles unknown phrasing and compound intent.
4. **Small prompt, bounded history.** A 4 GB GPU cannot afford an ever-growing transcript.
   Prompt size and history are treated as engineering constraints, not afterthoughts.
5. **Deterministic tests.** No test may use a real microphone, speaker, GUI, keyboard, or
   network. Audio is replayed from saved clips or synthetic frames; OS runners are mocked.
6. **One phase at a time.** Finish, verify, document, commit locally, then stop and give the
   user exact live-test steps.

---

## 2. Fixed decisions (do not change without the user's approval)

- Brain: LFM3-1.7B on llama.cpp. **Do not download another language model.**
- Server flags (leave untouched):
  `-ngl 99 --jinja -c 8192 --temp 0.1 --top-k 50 --repeat-penalty 1.1 --reasoning off
  --host 127.0.0.1 --port 9931`. `--reasoning off` is essential (~0.5 s replies instead of
  ~2.5 s).
- Ears: faster-whisper `base.en`, CPU, int8, beam 1, VAD on. Whisper always receives a
  contiguous float32 numpy array, never a file path (PyAV 19 breaks the path branch of
  faster-whisper 1.2.1).
- Mouth: Piper `en_US-lessac-medium`, CPU, `pw-play`.
- Default `BACKEND=local`; `BACKEND=gemini` remains a working rollback.
- Permission modes: `auto-safe` is the default; risky tools still ask; hands-free mode can
  never prompt and therefore refuses confirmation-tier calls.
- Never add powerful new tools (no arbitrary shell).
- System prompt and tool list stay short. Locally cap at 3 tool rounds per task.
- No sudo/dnf in automated work. If a command needs it, give the user the exact command and
  wait.

---

## 3. Architecture and data flow

### 3.1 A spoken command, end to end

```
microphone (PipeWire, pw-record)
  -> recorder / UtteranceDetector (energy + Silero VAD, 850 ms endpoint, 12 s cap)
  -> faster-whisper base.en (CPU int8, float32 array, initial_prompt/hotwords)
  -> wake word + phonetic repair (wake.py)
  -> fast_path match?
       yes -> ToolRegistry.execute -> code-built reply -> Piper
       no  -> AgentLoop -> LocalAdapter -> llama-server /v1/chat/completions
                 -> tool_calls -> ToolRegistry.execute (permission gate)
                 -> deterministic code reply, or one final model sentence
  -> Piper mouth (sentence streaming) -> speakers
  -> SESSION context + training dataset log
```

### 3.2 Main components

| Layer | Files | Responsibility |
|---|---|---|
| Entry point | `main.py` | `chat`, `gui`, `listen`, `wake-train`, `benchmark`, `doctor`, `self-test`, `voice-test`, `logs`, `stop`, `download-models`, and Phase 6 `teach` / `import-skills` |
| Config | `assistant/config.py`, `.env`, `.env.example`, `config/settings.yaml`, `config/apps.yaml` | Every runtime setting; `BACKEND`, model paths, voice thresholds, permission default |
| Brain | `assistant/brain/agent_loop.py`, `local_client.py`, `gemini_client.py`, `prompts.py`, `fast_path.py`, `tool_replies.py`, `reply_style.py`, `tool_output.py`, `context.py`, `server_manager.py`, Phase 6 `skills.py` | LLM calls, tool rounds, deterministic routing, replies, history, screen/session context |
| Tools | `assistant/tools/registry.py`, `files.py`, `apps.py`, `volume.py`, `web.py`, `desktop_control.py`, `screen_actions.py`, `sandbox.py` | Real OS actions plus schemas and permission gate |
| Vision | `assistant/vision/atspi_helper.py`, `screen_watcher.py` | AT-SPI2 tree walker (system Python), agent-desktop adapter, OCR fallback, continuous `@ref` watcher |
| Speech | `assistant/speech/recorder.py`, `vad.py`, `ears.py`, `mouth.py`, `wake.py`, `mic_input.py`, `denoise.py` | Capture, endpointing, STT, TTS, wake matching, mic gain |
| Interfaces | `assistant/interfaces/cli.py`, `gui.py`, `listen.py`, `voice.py`, `wake_train.py` | Chat loop, hands-free loop, voice controller |
| Safety | `assistant/safety/permissions.py`, `killswitch.py`, `audit.py`, `redact.py` | Permission tiers, kill switch, audit log, secret redaction |
| Tests | `tests/` | 415 deterministic tests at Phase 6 start |

### 3.3 Prompt discipline

Phase 7 makes the local system prompt **100 % static**: persona, rules and tool guidance
never contain the screen, skills, mode, roots or date. Everything volatile travels in one
`[Context: browser=…, site=…, app=…, focused=…, last_action=…]` tag prepended to the current
**user** message only, while `history` stores clean text. `local_client` pins
`id_slot: 0` and `cache_prompt: true`, so llama-server caches the prefix once in VRAM.

History is capped at three text-only turns (six messages); every registered desktop tool is
exposed to the local model, and generation is capped at 220 tokens with a
sentence-completion guard (`clean_llm_reply`). Phase 5 had reduced the first-call prompt from
**599 to 455 tokens** and the post-`list_files` call from **5 756 to 548 tokens** (−90 %);
Phase 6 capped history; Phase 7 removed the per-turn cache invalidation that had pushed a
live reply from 0.83 s to 5.16 s.

### 3.4 Safety model

- `auto-safe` runs read-only and declared-safe actions without asking.
- Confirm-tier tools, writes outside `ALLOWED_ROOTS`, post-untrusted-content writes, and
  suspicious tool names (delete/move/write/send/exec) ask the user (y/N, 60 s, default No).
- Hands-free `listen` sets `allow_prompt=False`: a would-be prompt becomes a refusal and
  Ultron says “That needs confirmation. Please type it in the chat.”
- Blocked: `.env`, `/etc/sudoers`, systemd/udev/polkit security files, GNOME Keyring and
  browser password stores, disabling SELinux/firewalld/polkit, editing `assistant/safety/*`.

---

## 4. Timeline — what was built, what broke, what fixed it

### Phase 0 — audit and backup (2026-10-30)

Read the Gemini-era code, explained the flow, and made a full backup at
`.backups/ultron-pre-local-20261030-152842.tar.gz`. No edits.

### Phase 1 — local brain (typed commands)

Created `assistant/brain/local_client.py` (`LocalAdapter`) with the same interface as the
Gemini adapter, OpenAI-format tool schemas, one JSON-retry on malformed tool arguments, and
a 3-tool-round cap. Added `volume_up` / `volume_down` with clamped 5–20 % steps and a hard
1.0 ceiling.

**Challenges:** the local model returned tool arguments as JSON strings; malformed JSON had
to be retried once with a corrective tool message. The adapter returns google-genai-shaped
objects so the existing AgentLoop needed no rewrite.

**Verification:** 20 tests passed; live reply turn 1.52 s; tool choice 1.55 s.

### Phase 2 — automatic llama-server lifecycle

Created `server_manager.py`: health-check first, reuse a server started by someone else,
spawn only if the port is free, wait ≤90 s, log to `logs/llama-server.log`, stop only our
own child, never start a second copy. 10 offline tests.

**Challenge:** distinguishing “our child”, “foreign server” and “port occupied by another
program”. `stop()` only terminates the child this process started.

### Phase 3 — local ears

Installed faster-whisper into the venv, downloaded `base.en` once into `models/`, then
loaded it offline. `WhisperEars` passes a float32 array, not a path, because `av` 19 rejects
the `metadata_errors` kwarg used by faster-whisper 1.2.1.

**Live learning:** a comma-separated hotword list was echoed into transcripts on unclear
audio (“list files, see in my download…”). Auto-built hotwords were removed; only an
explicit `WHISPER_HOTWORDS` is passed.

### Phase 4 — local mouth

Piper loads once, synthesizes per sentence, streams the first sentence while later ones
generate, writes 22 050 Hz mono, applies volume with a 0.95 peak ceiling, and is played by
`pw-play`. Ctrl+C stops playback and keeps the session.

**Challenge:** loading the voice per reply was the slow part; load-once plus sentence
streaming fixed it. `_play_wav_file` terminates a half-played player on interrupt.

### Phase 5 Part A — reply size, prompt size, volume, mic normalisation

- Compacted tool results before sending them back to the model.
- Capped local generation at 128 tokens first call / 96 after a tool.
- Added `set_volume(percent)` and `mute_audio(muted)`.
- Replaced auto hotwords with an optional explicit list.
- Normalised every recording to 70 % of full scale with gain capped by the true peak.

### Phase 5 Part B1 — listening reliability

Live evidence: 8 of 18 voice attempts rejected; three 8 s start timeouts; two full-scale
thumps counted as speech; a 30 s fan recorded as `max_length`.

**Root causes and fixes**

| Symptom | Root cause | Fix |
|---|---|---|
| 30 s fan treated as speech | Stream-opening burst polluted the 200 ms noise calibration (floor 23 186 → end threshold 41 734) | Adaptive warm-up discards 300–1500 ms; start requires 10 dB SNR over a rolling floor |
| “Say it again” on real speech | Guard rejected on one bad score; real clip was `avg_logprob=-1.087` | Log scores; reject only on two bad signals or one extreme (later tightened again in B1 polish) |
| First word lost | 600 ms discard after Piper | Keep last 400 ms and `feed_warm()` for start detection only |
| No command after “Yes?” | Follow-up window closed on audio time while the user was still speaking | Close only when the detector is idle |
| Double `pw-record` | `record_auto` started the source twice | Recorder owns start/stop |
| Garbled transcripts | Auto hotword list echoed as words | Remove auto hotwords; opt-in only |

### Phase 5 B2 — noise robustness, hands-free mode, fast path, web tools

- Fan analysis: 40 % of energy below 100 Hz, steady RMS ~393. A 10 dB SNR gate over a
  rolling floor prevents a steady fan from ever starting an utterance.
- Denoise A/B: clean clips 8/8 without denoise, 7/8 with denoise; noisy matches did not
  improve. Decision: `VOICE_DENOISE_STRENGTH=0` by default, kept opt-in.
- Added `main.py listen` with a wake word, early endpoint, follow-up window, and
  `allow_prompt=False`.
- Added `fast_path.py` for whole-sentence commands and `web.py` for search.
- DuckDuckGo’s HTML endpoint returned a bot challenge (HTTP 202), so “open first result”
  could not be proven live; the fallback search page ran instead.

### Phase 5 B3 — wake word, timing, privacy

- Live wake attempts: `outron.`, `I'm drawn.`, `I draw`, `I'll do it.`, `I'll turn.` — only
  `outron.` matched.
- Added adaptive warm-up, a leaky speech-activity counter, per-frame SNR, and built-in
  aliases from measured clips.
- Added `main.py wake-train` to record 16 cues and compare plain/hotwords/no-VAD variants.
- Fixed the follow-up window race and added `ContinuousReader` so the mic pipe is always
  drained.
- Privacy: ignored speech is never logged as text and never saved.

**Measured wake table (16 saved clips):** plain + aliases 8/16, hotwords + aliases 12/16.
“Hey Ultron” remained weak (0/4), which is why B6 and Phase 6 add aliases and lexical
priming.

### Phase 5 B4 — live bug fixes

- Sub-second endpoint: active speech level + 900 ms silence (later 850/1350 ms).
- Wake aliases for `open 5ox`, `fire box`, `I'll draw …`.
- `close_app` tool with a strict process allowlist; `open_app` no longer claims success for
  unknown names.
- `/voice` speaks every outcome, including “I didn’t hear anything.”
- First-link routing is never mistaken for `open_app`.

### Phase 5 B5 + Part C — full desktop control and benchmark

- Browser targeting remembers `SESSION.last_browser`; an explicit browser is launched
  directly, never through `xdg-open`.
- Dynamic `.desktop` scan found **176** apps on this machine; fuzzy + Soundex matching.
- Added `type_text`, `scroll_page`, `browser_tab_control`, `window_control`,
  `set_brightness`, `media_control`, `system_info`, `take_screenshot`.
- `ydotoold` starts once per session on `/tmp/.ultron_ydotool.sock`; PageDown/PageUp replaced
  arrow spam so `^[[B` no longer appears in the terminal.
- 25-command benchmark, mocked OS and LLM: 25/25, average 1.519 ms.

**Live problems:** 7 of 8 utterances ended `reason=early` (600 ms was too aggressive);
“open Firefox and search github” opened the search in Brave because `browser=null`; apps
outside `apps.yaml` were unknown; “Hello” took 2.69 s.

**Fixes:** two-speed endpoint, browser memory, dynamic app scan, lean chat request.

### Phase 5 B6 — neural VAD, one-call brain, smart control

- Silero VAD wrapper; adaptive 850 ms endpoint; 12 s cap; rolling noise floor.
- One LLM call when every tool has a deterministic reply; compact schemas (8 tools,
  ~370 estimated tokens).
- Natural tab phrasings, web-app fallback for 14 services, fast scroll/type.
- Wake aliases and error-loop guard: “I didn’t hear anything.” can never match a learned
  alias again.
- 53 new tests in `test_b6_smart_brain.py`; total **415 passed**; benchmark **25/25**.

**State at the end of B6:** branch `phase5-b6-smart-brain`, HEAD `8e2646f`, clean tree, no
server or `ydotoold` left running.

---

## 5. Live-failure catalogue (the collective memory)

| # | Live symptom | Evidence | Root cause | Fix | Pinned by |
|---|---|---|---|---|---|
| 1 | 30 s recording of a fan | `reason=max_length`, noise_floor 23186 | Start-of-stream burst calibrated the floor; end threshold above int16 max | Adaptive warm-up; SNR gate; rolling floor | `test_warmup_snr.py`, B6 VAD tests |
| 2 | “I didn’t hear anything.” triggered wake loop | Whisper error string matched learned alias `i didn` at 0.86 | Error strings were passed to `wake.py` | Never pass rejected/empty text; clean learned aliases | `test_listen.py`, B6 |
| 3 | Wake word missed as “outron / I’m drawn / I’ll turn” | 18:01 wake-train clips | base.en is weak on “Ultron” | Safe/risky aliases, prefix stripping, wake-train, Phase 6 lexical prompt | `test_wake.py`, `test_wake_train.py` |
| 4 | First word lost after Piper | Command started 200–500 ms after speech | 600 ms discard window | Keep 400 ms, `feed_warm`, close only when idle | `test_listen_timing.py`, `test_listen_reader.py` |
| 5 | Command after “Yes?” ignored | Window closed at 9440 ms mid-sentence | Window closed on audio time | Close only when detector idle | `test_listen_timing.py` |
| 6 | Garbled transcripts from hotword list | “list, list files…”, “mute, mute, unmute” | Auto hotwords echoed on unclear audio | Remove auto list; opt-in `WHISPER_HOTWORDS` | `test_ears.py` |
| 7 | Clipped mic even at peak 24392 | After peak 32767, gain 2.10x | Gain cap used 95th percentile | Gain capped by true raw peak | `test_mic_level.py` |
| 8 | `//voice` reached the LLM | “Voice is active at the current volume” | Only one leading slash recognized | Normalize slashes; unknown slash never reaches LLM | `test_chat_commands.py` |
| 9 | “stay silent for 10 seconds” claimed a timer | “Audio is muted for 10 seconds.” | No timer exists; model invented it | Timed-mute refusal + “yes” mutes now | `test_volume.py`, `test_agent_rounds.py` |
| 10 | Search opened in Brave, not Firefox | `web_search` with `browser=null` | `xdg-open` default browser | Browser memory + explicit executable | `test_b5_full_control.py` |
| 11 | `open_first_result` opened a blank page | Google `btnI=1` redirect page | Redirect notice/blank in Firefox/Brave | Phase 6: canonical sites or clean search page; never `btnI` | Phase 6 tests |
| 12 | `"search f1 in youtube"` searched Google | Tool call `open_first_result("f1")` | “youtube” treated as browser name; no site search | Phase 6 site search + in-tab navigation | Phase 6 tests |
| 13 | Brave WebRTC AGC blew out the mic | `12:37:40 peak=32768 rms=10659…14064 floor=2616` | Chromium boosted PipeWire source gain | Phase 6 AGC guard restores 0.21 after a hot utterance | Phase 6 tests |
| 14 | Multi-turn slowdown | history grows with tool JSON | Every turn re-evaluates a larger prompt | Phase 6 history cap (3 turns) + text-only pruning | Phase 6 tests |
| 15 | Dry robotic replies | “Firefox is open.”, “Closed the tab.” | Code replies were terse | Phase 6 natural professional replies | Phase 6 tests |
| 16 | 9 pytest “errors” in permission modes (user report Nov 2/3) | `415 passed, 9 errors` in `test_permission_modes.py` | **Not reproducible at `8e2646f`:** the file passes 12/12 and the full suite 415/415 | Phase 6 audits config/state isolation and adds a guard | Phase 6 tests / final report |
| 17 | `wmctrl -l` returned 0 windows on GNOME Wayland | `"windows": [], "browser": "", "site": ""` | Native Wayland windows are invisible to X11 tools | Phase 7 `org.gnome.Shell.Introspect.GetWindows` + wmctrl + process/session fallback | `test_phase7_brain_wayland.py` Part 1 |
| 18 | Live reply grew 0.83 s → 5.16 s | every turn mutated `Screen:` in the system prompt | The KV cache was invalidated at token 0 | Phase 7 static system prompt + `[Context: ...]` on the user message + `id_slot:0`, `cache_prompt:true` | `test_phase7_brain_wayland.py` Part 2 |
| 19 | “write what is an AI agent inside this new document” typed the instruction literally / refused >200 chars | old `type_text` capped at 200 and no document-intent rule | Dictation and generation shared one path; split-brain history had no previous turn | Phase 7 LLM generation rule + `wl-copy`/Ctrl+V paste up to 4000 chars + `record_external_turn` | `test_phase7_brain_wayland.py` Parts 2/3/5 |
| 20 | `close GitHub` said “couldn't find an app” | GitHub is a website, not a desktop app | No tab-close routing | Phase 7 web-only site → `browser_tab_control(close_tab)`, reply “I've closed the GitHub tab.” | `test_phase7_brain_wayland.py` Part 6 |
| 21 | “set a timer for 5 seconds” had no real timer | no timer tool existed | Fast path had no duration parser | Phase 7 `set_timer` + background `threading.Timer` + `notify-send` + Piper | `test_phase7_brain_wayland.py` Part 6 |
| 22 | Flatpak Discord `Uncaught exception: Error: write EPIPE at console.warn` (19:20:19) | Electron/Flatpak launched with `stderr=PIPE`; `pkill` killed zypak before Electron exited | Detached GUI children must never own a pipe; Flatpak must be stopped as a sandbox | Phase 7 all-DEVNULL launches + `flatpak kill <id>` first + XWayland `wmctrl -c` + process-group SIGTERM | `test_phase7_brain_wayland.py` EPIPE tests |
| 23 | `"is stop listening"` did not stop hands-free mode | wake stripping left the filler “is” | Stop check compared the raw remainder | Phase 7 stops after stripping `is/please/now/just/...` | `test_phase7_brain_wayland.py` Part 6 |
| 24 | GNOME 50.5 window introspection returned `AccessDenied` | `gsettings get org.gnome.shell introspect` → `No such key`; `GetWindows is not allowed` | The Phase 7 gdbus introspection key was removed in GNOME 50 | Phase 8 uses AT-SPI as the primary eyes and reports `introspect: false` honestly | `test_phase8_eyes_hands_ears.py` Part 1 |
| 25 | OCR layer could not run live | `shutil.which("tesseract")` empty | Tesseract is not installed on this machine | OCR parser/fallback implemented and unit-tested; user can `sudo dnf install tesseract` | Phase 8 OCR tests |
| 26 | small.en slower than the 0.55–0.75 s target and one clip regressed | measured 1.75–2.08 s per saved clip; `mute unmute` → `Mute on mute.` rejected | small.en + beam 2 is ~4× base.en beam 1; the repetition guard correctly refuses the wrong decode | Settings kept as requested and measured honestly; `WHISPER_MODEL=base.en` remains the latency fallback | Phase 8 ears tests + PROGRESS table |
| 27 | `close_app` left Calculator, Text Editor and Settings open | live: `gnome-calculato` 15843, `gnome-text-edit` 15943, `gnome-control-c` 16169; `close_app` → `running=False, closed=False` | Linux truncates `/proc/<pid>/comm` to 15 chars; `pgrep -x <longer name>` refuses and returns zero matches | `_process_names_for()` appends the 15-char alias | `test_process_names_include_the_linux_15_char_comm_alias`, `test_close_app_finds_a_process_name_truncated_to_15_chars` |
| 28 | `close_app("lutris")` reported not running while the Flatpak was running | live `flatpak ps` → `net.lutris.Lutris`; `close_app` → `running=False` | `_default_flatpak_runner` used `stdout/stderr=DEVNULL`, so `_flatpak_running_apps()` always parsed nothing | capture `flatpak ps` stdout/stderr (short-lived CLI; all-DEVNULL still applies to detached GUI launches) | `test_default_flatpak_runner_captures_ps_output` |

**Honesty note on #16:** the coding session on 2026-11-03 could not reproduce the 9 errors.
The exact command `.venv/bin/python -m pytest tests/test_permission_modes.py -v` returned
`12 passed in 0.72s`, and `.venv/bin/python -m pytest -q` returned `415 passed in 46.79s`.
The most likely explanation is a different working state or environment at the time of the
user's run. It is recorded here as a real report, not dismissed.

---

## 6. Phase 6 — Screen-Aware LLM Brain, In-Tab Search, AGC Guard & Skill Training (Nov 3)

Branch: `phase6-intelligent-agent` (from `phase5-b6-smart-brain` @ `8e2646f`).

| Part | What | Status |
|---|---|---|
| 1 | Fix the 9 reported permission-test errors; prove 0 errors | DONE (not reproducible; 12/12, full suite 450 passed) |
| 2 | Dynamic mic AGC guard + Whisper lexical priming + phonetic repair | DONE |
| 3 | `wmctrl -l` screen awareness, Google default, site/in-tab search | DONE |
| 4 | LLM-first brain, history cap, professional replies | DONE |
| 5 | Trainable skills + experience dataset | DONE |
| 6 | Comprehensive tests + benchmark | DONE (450 passed, benchmark 25/25) |
| 7 | Docs, final verification, local commits | DONE (see final handoff) |

### 6.1 Part 1 — permission tests

**Reported:** 9 errors in `tests/test_permission_modes.py` during a B6 run, including
`test_confirm_tier_tools_still_ask` and `test_untrusted_content_still_forces_confirmation`.

**Investigation (2026-11-03):**

- `git branch --show-current` → `phase5-b6-smart-brain`; `HEAD` → `8e2646f`.
- `git status --short` → clean.
- `.venv/bin/python -m pytest tests/test_permission_modes.py -v` → **12 passed**.
- `.venv/bin/python -m pytest -q` → **415 passed**.
- The tests use an explicit `confirm_callback`, so they cannot read stdin. `PermissionGate`
  falls back to config `auto-safe`. No test weakens a permission check.

**Result:** not reproducible at HEAD. The permission tests were left untouched; no check
was weakened. Phase 6 adds `test_permission_modes_run_with_zero_errors`, which reruns the
whole module in a subprocess and asserts `12 passed, 0 errors`. Verified again after all
Phase 6 changes: **12 passed**.

### 6.2 Part 2 — AGC guard, lexical priming, phonetic repair

Design:

- `assistant/speech/mic_input.py`: `agc_guard_needed(peak, rms)` is true only for
  `peak >= 32700` AND `rms > 3500`. `restore_mic_volume()` runs
  `wpctl set-volume @DEFAULT_AUDIO_SOURCE@ <MIC_SOURCE_VOLUME or 0.21>`.
  `guard_mic_agc()` runs it in a daemon thread in production; tests pass a fake runner or a
  synchronous flag. Tests can never change the real mic (`PYTEST_CURRENT_TEST` guard).
- `VoiceController._finish_recording` calls the guard with the raw peak/RMS before
  normalisation.
- `run_listen` calls it on the raw utterance PCM.
- `web.open_url` and browser launches schedule a 1.5 s restore so a browser start cannot
  leave the source boosted.
- `ears.py`: default `WHISPER_INITIAL_PROMPT` (natural sentence, ~155 chars) primes proper
  nouns. Config field `whisper_initial_prompt`, env `WHISPER_INITIAL_PROMPT`; an explicit
  empty value disables it.
- `wake.py`: add `hey everyone`, `iron`, `here drawn`, `altron`, `all drawn` as risky wake
  aliases (only trusted when followed by a real command or alone). Add phonetic repairs:
  `third/hurt/sir youtube` → `search youtube`; `right <text>` → `write <text>` in a typing
  context.

**Measured result (2026-11-03, real base.en on the saved clips):**

- The prompt is **153 characters**. It is passed only when non-empty; `WHISPER_INITIAL_PROMPT=`
  disables it (tested).
- 8 valid command clips: **8/8 semantically correct with and without the prompt**. The
  prompt changed punctuation/capitalisation on one clip (`voice, please`), not meaning.
- 8 single-“Ultron” wake clips: **6/8 matched without prompt → 8/8 with prompt**. Two clips
  that the guard rejected became a clean `Ultron`.
- 4 “Hey Ultron” clips: **2/4 both ways**; the prompt did not repair the other two.
- No comma-list echo returned; the longest prompt-enabled wake transcript was <120 chars.
- AGC: the deterministic guard test confirms `[[wpctl, set-volume,
  @DEFAULT_AUDIO_SOURCE@, 0.21]]` for `peak=32768, rms=14064`, and no call for
  `peak=32700, rms=3500` or normal speech. `VoiceController._finish_recording` is tested to
  invoke it on a hot recording. Tests cannot touch the real mic.

### 6.3 Part 3 — screen awareness and in-tab search

Design:

- `assistant/brain/context.py`: `open_windows`, `active_site`, `last_site`, and
  `get_screen_context()` running `wmctrl -l` with a 0.2 s timeout. Recognizes YouTube,
  GitHub, Google, Instagram, Discord, VS Code, Firefox, Brave, Chrome, Reddit, WhatsApp,
  Netflix, Spotify, ChatGPT, Gmail, Amazon, LinkedIn, Twitter/X.
- `web.py`: default `SEARCH_ENGINE=google`; `web_search(query, browser=None, site=None,
  in_current_tab=False)` builds site URLs:
  YouTube `https://www.youtube.com/results?search_query=<q>`,
  GitHub `https://github.com/search?q=<q>`,
  Reddit, Wikipedia, etc.
- “search f1 in youtube” → `site=youtube`; “search f1” while YouTube is the active site →
  YouTube search too.
- In-tab navigation: focus the browser with `wmctrl -a`, then Ctrl+L, `wl-copy` + Ctrl+V,
  Enter via `ydotool`; fall back to launching the URL if injection is unavailable.
- `open_first_result` never uses `btnI=1`. Known brands open the canonical site; general
  queries use a clean first-result fetch or the Google search page.
- “play/click/open first video” on YouTube opens the first parsed video when possible,
  otherwise the YouTube search results page.

**Verified result:**

- `wmctrl -m` on this laptop reports GNOME Shell with exit 0. `wmctrl -l` returned **no
  titles at the time of the check** because no GUI windows were open; the parser and
  session update are covered by deterministic tests with synthetic titles.
- `search f1 in youtube` → `{"query": "f1", "site": "youtube"}` and
  `https://www.youtube.com/results?search_query=f1`; reply
  “Searching YouTube for f1 right now.”
- With `SESSION.active_site="youtube"` and `last_browser="firefox"`, `search f1` is routed
  to the **current tab**; `navigate_current_tab` is tested to send Ctrl+L, Ctrl+V and
  Enter through the injected `ydotool` runner.
- `open_first_result("github")` opens `https://github.com` with method `canonical`; a
  general query uses the clean fetch or the Google results page. No `btnI` exists in the
  tested URLs.

### 6.4 Part 4 — LLM-first brain and professional replies

**Measured prompt size (deterministic stub; chars/4 estimate, not llama-server tokens):**

| Point | Messages sent | JSON chars | Estimated tokens |
|---|---|---|---|
| Turn 1 (chat) | system + 1 | 375 | 94 |
| Turn 15 (chat) | system + 6 | 580 | 145 |
| Turn 14 = Turn 15 | system + 6 | 580 | 145 |
| Compact tool schemas (tools on) | constant | 1,967 | 492 |

So the static part never changes and history **plateaus after Turn 4**; it no longer grows
with the number of commands. Actual `llama-server` prompt-token counts are **UNVERIFIED**
in this offline run.

- `AgentLoop.run_task` becomes a wrapper around the existing loop; a `finally` prunes
  history to the last **3 turns / 6 messages** and keeps only text parts (user text + spoken
  reply). Tool-call JSON never survives into the next turn.
- Every local LLM call receives one compact line:
  `Screen: browser=firefox, site=youtube, windows=[YouTube — Mozilla Firefox; ...]`.
- `prompts.py` tells the model that input is STT, may contain phonetic mishearings, and that
  `Screen:` is live truth. Tool names include `web_search(query, site, browser)` and
  `learn_skill`.
- `tool_replies.py` becomes warmer and more professional:
  “I’ve opened Firefox for you.”, “Searching YouTube for F1 right now.”,
  “I’ve closed that tab.”, “I’ve scrolled the page down.”,
  “I’ve typed that into the active window.” One sentence each.

### 6.5 Part 5 — trainable skills and experience dataset

- `assistant/brain/skills.py`, backed by `config/learned_skills.json` (shipped) and
  `workspace/learned_skills.json` (user-taught, git-ignored; workspace wins).
- Voice teaching: “remember when I say <trigger>, <action>” and
  “learn skill <trigger> means <action>”.
- CLI: `main.py teach "<trigger>" "<action1>; <action2>"` and
  `main.py import-skills <path.json>`.
- Matching is normalized exact + fuzzy + keyword; a match executes the action chain through
  the fast path / agent loop. The top 2 relevant skills are also injected into the LLM
  prompt as hints.
- Experience log: `workspace/training_dataset.jsonl` (git-ignored, privacy-safe) records
  only accepted wake commands:
  `{timestamp, raw_transcript, repaired_transcript, screen_context, tools_called, reply,
  latency_ms}`.

### 6.6 Part 6 — tests and benchmark

Planned `tests/test_phase6_intelligent_agent.py`:

1. Permission module: all 9 previously named tests exist and the module passes.
2. AGC guard: hot utterance calls `wpctl set-volume … 0.21`; normal audio does not.
3. “search f1 in youtube” → `web_search(query="f1", site="youtube")` with the YouTube URL;
   “search f1” with YouTube on screen does the same; no `open_first_result`.
4. “Third YouTube” / “Iron Search for YouTube” repair and route.
5. “Hey everyone open firefox” / “Iron open firefox” wake matches; “Hey Ultron” still does.
6. History after 15 simulated turns is ≤6 messages and prompt size is flat.
7. Teaching “when I say work mode, open code and open github” persists and runs both tools.

**Verified result:** `tests/test_phase6_intelligent_agent.py` has **31 tests** and all pass.
The permission subprocess test reports `12 passed`. The 15-turn history test asserts
`len(loop.history) <= 6` after every turn and the largest prompt the adapter saw is
6 messages with a constant system-prompt length. The skill test persists “work mode” and
executes both actions.

### 6.7 Part 7 — final verification

- `.venv/bin/python -m pytest -q` → **450 passed, 0 failed, 0 errors** (38.98 s final run;
  39.19 s first Phase 6 run).
- `.venv/bin/python main.py benchmark` → **25/25 passed**, average **2.063 ms** (mocked;
  live mic/LLM latency UNVERIFIED).
- `main.py teach` and `main.py import-skills` verified in an isolated
  `ULTRON_WORKSPACE_DIR`: both skills were written to `workspace/learned_skills.json`.
- `compileall` OK; `git diff --check` clean.
- Commits on `phase6-intelligent-agent`: `07781dc` (parts 1–2), `2e0bc2a` (part 3),
  `16a8448` (parts 4–5), `a970252` (part 6), `8350d0e` (part 7 docs), `9d2ee28`
  (head state), `67d52fa` (flat-prompt verification), plus the final docs commit.
- Final checks: `ss -ltnp | grep 9931` → empty; `pgrep -a ydotoold` → empty; working tree
  clean; `.env` never staged.

---

## 6A. Phase 7 — Wayland Screen Sensor, Static KV Cache, Writing, Timers & Flatpak EPIPE (Nov 3)

Branch: `phase7-llm-brain-wayland` (from `phase6-intelligent-agent` @ `3ca55ba`).
Implementation commit: `7306554`. Full details and the live-test checklist are in
`docs/PROGRESS.md`; this section is the long-term summary.

| Part | What | Result |
|---|---|---|
| 1 | GNOME Wayland 3-layer screen sensor | DONE — `ensure_wayland_introspect`, gdbus Introspect (0.35 s), wmctrl (0.2 s), process+session fallback; `focused_window/focused_app/last_reply/last_typed_text`; Claude site |
| 2 | Static KV prefix and full tool arsenal | DONE — 100 % static system prompt, `[Context: ...]` tag on the user message, all 22 registered tools, `id_slot:0`, `cache_prompt:true`, `max_tokens:220`, `clean_llm_reply`, `record_external_turn` |
| 3 | Natural executive brain | DONE — persona/writing/system/timer/tab rules, `ULTRON_BRAIN_REPLIES=auto`, instant repeat from `SESSION.last_reply`, warm code replies |
| 4 | Google + site/in-tab search + first result | DONE — Google default, `parse_search_request`, Ctrl+L/Ctrl+V/Enter with 60/40 ms beats, expanded canonical brands, no `btnI`/`!ducky` |
| 5 | Instant clipboard typing | DONE — 4000-char cap, `wl-copy -- <text>` + Ctrl+V, `--key-delay 8` fallback, `last_typed_text`, `write it`/`right it` |
| 6 | Timers, tab close, aliases, stop phrase | DONE — `set_timer` background thread + notification + Piper, web-only close → Ctrl+W, Lutris/text-editor aliases, `is stop listening` |
| 7 | Skills/memory/dataset | DONE — alias + fact teaching in `workspace/user_memory.json`, `enter the email`, `main.py export-dataset` ChatML JSONL |
| 8 | Tests and benchmark | DONE — 53 new tests, full suite **503 passed**, benchmark **25/25**, average **1.559 ms** (mocked) |
| + | Electron/Flatpak EPIPE | DONE — every GUI `Popen` uses all-DEVNULL + new session + close_fds; `flatpak kill` before pkill; tests pin it |

**Why `assistant/llm/llama_server.py` was not created:** this codebase has no
`assistant/llm/` package. The actual HTTP transport to `llama-server` lives in
`assistant/brain/local_client.py`, so the required `id_slot`, `cache_prompt`, `max_tokens`
and sentence guard were implemented there. No duplicate transport was added.

**Tool-list note:** `press_key` was listed in the Phase 7 request but did not exist. It was
added as a strictly allow-listed navigation tool (enter, tab, escape, space, backspace,
delete, arrows, home/end, page keys, F11) rather than arbitrary keyboard injection.

### 6A.1 Verification (actually run)

| Check | Command | Result |
|---|---|---|
| Full suite | `.venv/bin/python -m pytest -q` | **503 passed** (38.82 s) |
| Phase 7 module | `pytest tests/test_phase7_brain_wayland.py -q` | **53 passed** |
| Benchmark | `.venv/bin/python main.py benchmark` | **25/25**, average **1.559 ms** (mocked) |
| Compile | `python -m compileall -q assistant main.py scripts tests/test_phase7_brain_wayland.py` | OK |
| Whitespace | `git diff --check` | clean |
| No llama-server | `ss -ltnp \| grep 9931` | empty |
| No ydotool daemon | `pgrep -a ydotoold` | empty |
| Live mic/speakers/GUI | — | **UNVERIFIED** (no hardware in the coding session) |

### 6A.2 Known limitations added by Phase 7

- Real GNOME introspection, `ydotool` key injection, `wl-copy` paste and in-tab browser
  navigation are offline-tested only; the live checklist in `docs/PROGRESS.md` is the proof.
- `gdbus`/`wmctrl`/`pgrep` screen probes run once per task in production (roughly 40 ms on
  this laptop); tests and the benchmark mock them so they stay deterministic.
- `press_key` is intentionally limited to navigation keys; it cannot type arbitrary text or
  press destructive shortcuts.
- The exported ChatML dataset reflects what the experience log records (transcript, tool
  names, reply); it does not contain hidden reasoning or audio.

---

## 6B. Phase 8 — Real-Time Eyes & Hands, small.en Ears, Universal `gio launch` (Nov 5)

Branch: `phase8-eyes-hands-ears` (from `phase7-llm-brain-wayland` @ `242a57f`). Full
evidence, measured numbers and the live checklist are in `docs/PROGRESS.md`; this section is
the long-term summary.

| Part | What | Result |
|---|---|---|
| 1 | `agent-desktop` + GNOME AT-SPI accessibility + Chromium renderer accessibility | DONE — `agent-desktop@1.2.0` installed in `~/.npm-global` without sudo; `ACCESSIBILITY_ENABLED=1` / `QT_ACCESSIBILITY=1`; `ensure_accessibility()`; `--force-renderer-accessibility` for Brave/Chrome |
| 2 | 3-layer eyes + continuous `ScreenWatcher` | DONE — persistent `/usr/bin/python3` AT-SPI helper (`@e1..@eN`, bounds, editable, actions), agent-desktop adapter, OCR fallback, 1.2 s daemon, `| Visible UI: ...` on the user turn |
| 3 | 3-tier hands | DONE — AT-SPI action, `ydotool mousemove --absolute <x> <y>` + `click 0xC0`, browser `Ctrl+F → label → Esc → Enter`; `read_screen`; `focus_and_type` |
| 4 | `small.en` + screen-primed listening | DONE — CPU int8, `cpu_threads=6`, `num_workers=2`, beam 2, best_of 2, VAD 250/200 ms, dynamic hotwords, phonetic repairs; measured **1.75–2.08 s/clip**, not the 0.55–0.75 s target |
| 5 | Universal app launcher | DONE — `desktop_path`/`desktop_id`, `gio launch` → `gtk-launch` → `flatpak run` → `Exec`, 177 indexed apps, full Fedora alias table, `switch_app`/`focus_target_window` raise via gio; live launch verified for calculator, text editor, files, terminal and settings |
| 6 | Tests, benchmark, docs | DONE — **37** Phase 8 tests, full suite **540 passed**, benchmark **25/25** at **1.480 ms** (mocked) |

**Live findings that matter:**

- **GNOME 50.5 removed the introspection key.** `gsettings get org.gnome.shell introspect`
  → `No such key "introspect"`, and `org.gnome.Shell.Introspect.GetWindows` →
  `AccessDenied`. The Phase 7 gdbus window sensor is therefore broken on this machine;
  Phase 8's AT-SPI bus is the working eye. `ensure_accessibility()` reports the failure
  instead of hiding it.
- **AT-SPI is fast enough.** The targeted Nautilus walk measured **206 nodes / 14 elements
  in 112.5 ms**; the full desktop walk was 2,001 nodes / 296 ms. The helper caps nodes/depth
  and ranks interactive roles. The live `read_screen()` returned the real `Downloads`
  window with 14 refs; the live watcher refreshed and stopped cleanly.
- **agent-desktop works too.** `list-windows` and `snapshot --app nautilus` succeeded; the
  adapter parsed 21 elements in 337 ms. It is used only when the native helper is
  unavailable, because both read the same bus.
- **GTK4 bounds bug handled.** SCREEN coordinates return `x=0,y=0` for children; WINDOW
  coordinates plus the frame origin give the real centre (verified on the Back button).
- **Tesseract is not installed**, so live OCR is UNVERIFIED. The screenshot + TSV parser and
  the click-from-OCR path are unit-tested with injected runners. Continuous watching never
  screenshots; OCR happens only for an explicit `read_screen`/`click_element`.
- **ydotool syntax corrected.** v1.0.4 takes positional coordinates
  (`ydotool mousemove --absolute 100 100`); the `-x/-y` flags in the request do not exist.
- **small.en is slower than requested.** Model load 0.91 s; transcription 1.75–2.08 s per
  saved clip (3.1–10.8 s audio), vs base.en beam 1 at ~0.4 s. The requested settings are
  kept and measured; `WHISPER_MODEL=base.en` is the latency fallback. One saved clip
  (`mute unmute`) decodes as `Mute on mute.` and is correctly rejected by the repetition
  guard; 7 of 8 speech clips pass (base.en: 8/8).
- **Environment shadowing:** this session inherited `WHISPER_MODEL=base.en` from the shell;
  `load_config` gives environment variables precedence over `.env` by design. Run
  `unset WHISPER_MODEL WHISPER_MODEL_DIR` before a live small.en test.

**Live GUI verification (2026-11-05, this branch):** the real `gio launch` path opened
Calculator, Text Editor, Files, Terminal and Settings; each window appeared, AT-SPI returned
10–38 elements, and `close_app` removed it. Lutris started as a Flatpak (`flatpak ps` showed
`net.lutris.Lutris`) but mapped no window within 40 s, so its spoken step stays UNVERIFIED.
Two live bugs were found and fixed: `pgrep -x` cannot match a process name longer than
Linux's 15-character `comm`, so `close_app("calculator"/"text editor"/"settings")` left the
windows open (fixed by adding the truncated alias in `_process_names_for`); and
`_default_flatpak_runner` sent `flatpak ps` stdout to `DEVNULL`, so the running-Flatpak
check always saw nothing (fixed by capturing the short-lived CLI output). Three new tests
pin both fixes; the full suite is **540 passed**.

**UNVERIFIED (needs Nitin):** real semantic clicks, real coordinate clicks, the browser
`Ctrl+F → Esc → Enter` activation, typing into real fields, launching/raising YouTube and
Lutris (the other five apps are verified), the live microphone and the speakers. No
automated test may press keys, click a window or use audio hardware.

---

## 7. Verification ledger (Phase 6/7/8)

> Every number here must come from an actual command run in this session. Anything not run
> is labelled UNVERIFIED.

| Check | Command | Result |
|---|---|---|
| Permission file at HEAD | `.venv/bin/python -m pytest tests/test_permission_modes.py -v` | **12 passed** |
| Permission file after Phase 6 | rerun in `tests/test_phase6...` subprocess | **12 passed, 0 errors** |
| Full suite at HEAD | `.venv/bin/python -m pytest -q` | **415 passed** (46.79 s) |
| Full suite after Phase 6 | `.venv/bin/python -m pytest -q` | **450 passed** (39.19 s) |
| Phase 6 tests | `tests/test_phase6_intelligent_agent.py` | **31 passed** |
| Benchmark after Phase 6 | `.venv/bin/python main.py benchmark` | **25/25 passed**, avg **2.063 ms** (mocked) |
| Full suite after Phase 7 | `.venv/bin/python -m pytest -q` | **503 passed** (38.82 s) |
| Phase 7 tests | `tests/test_phase7_brain_wayland.py` | **53 passed** |
| Benchmark after Phase 7 | `.venv/bin/python main.py benchmark` | **25/25 passed**, avg **1.559 ms** (mocked) |
| Phase 7 compile | `python -m compileall -q assistant main.py scripts tests/test_phase7_brain_wayland.py` | OK |
| Phase 7 diff check | `git diff --check` | clean |
| Full suite after Phase 8 | `.venv/bin/python -m pytest -q` | **540 passed** (53.50 s) |
| Phase 8 tests | `tests/test_phase8_eyes_hands_ears.py` | **37 passed** |
| Benchmark after Phase 8 | `.venv/bin/python main.py benchmark` | **25/25 passed**, avg **1.480 ms** (mocked) |
| Live AT-SPI read | `.venv/bin/python` → `read_screen()` | 14 elements from the real `Downloads` window, `focused_app=files` |
| Live watcher | 0.5 s interval for 2 s | 14 elements refreshed; thread stopped; no helper left |
| Live agent-desktop | `list-windows` + `snapshot --app nautilus` | window found; adapter parsed 21 elements in 337 ms |
| Live app launch (5) | `open_app` → real `gio launch` | Calculator, Text Editor, Files, Terminal, Settings opened; AT-SPI 10–38 elements; `close_app` removed each window |
| Live Lutris | `open_app("lutris")` + 40 s wait | Flatpak process started (`flatpak ps`), **no window**; `close_app` stopped it |
| Live Flatpak detection | `_flatpak_running_apps()` after the fix | `{'net.lutris.Lutris'}`; `close_app("lutris")` → `flatpak kill`, `closed=True` |
| small.en real clips | offline production options | 7/8 speech clips accepted; 1.75–2.08 s each; 0.91 s load |
| GNOME introspection | `gsettings get org.gnome.shell introspect` | **No such key** (GNOME 50.5); `GetWindows` → `AccessDenied` |
| OCR availability | `command -v tesseract` | **missing** (live OCR UNVERIFIED) |
| No llama-server | `ss -ltnp \| grep 9931` | verified empty in final report |
| No ydotool daemon | `pgrep -a ydotoold` | verified empty in final report |
| No AT-SPI helper | `pgrep -af atspi_helper.py` | verified empty in final report |
| Real saved clips | offline Whisper comparison | **8/8 semantic**; single wake **6/8 → 8/8**; “Hey Ultron” **2/4 both** |
| Live screen read | `wmctrl -m` / `wmctrl -l` | GNOME Shell responds; `-l` empty because no GUI windows were open |
| No llama-server | `ss -ltnp \| grep 9931` | verified empty in final report |
| No ydotool daemon | `pgrep -a ydotoold` | verified empty in final report |
| Working tree | `git status --short` | clean after the final commit |

---

## 8. Known limitations and UNVERIFIED items

- The live microphone and speakers cannot be exercised by the coding agent. Every voice fix
  is offline-tested; user live tests are the final proof.
- `HEY ULTRON` is the hardest wake phrase for `base.en`; lexical priming and aliases help
  but do not guarantee it.
- The 1.7B model is weak at long multi-step plans. The 3-tool-round cap is deliberate.
- `set_brightness` may fail honestly on this machine: GNOME exposes Keyboard.Brightness but
  not Screen.Brightness, and `/sys/class/backlight` is root-only.
- Playwright-style browser automation is not installed. In-tab navigation relies on
  `wmctrl` + `ydotool`; actual key injection is **UNVERIFIED** in automated tests by design.
- Phase 7 native GNOME introspection (`gsettings`/`gdbus`), real `wl-copy` paste,
  `flatpak kill`, and document writing are offline-tested only; the live checklist in
  `docs/PROGRESS.md` is the proof.
- **Phase 8:** GNOME 50.5 has no `org.gnome.shell introspect` key and denies
  `GetWindows`, so the AT-SPI bus is the real screen sensor. `ensure_accessibility()` says
  so honestly.
- **Phase 8:** live semantic clicks, coordinate clicks, the browser
  `Ctrl+F → Esc → Enter` activation, and typing into real fields are **UNVERIFIED**. The
  code paths are unit-tested with mocked `ydotool`/AT-SPI runners only.
- **Phase 8:** live app launching is verified for calculator, text editor, files, terminal
  and settings (window + AT-SPI + close). Lutris starts as a Flatpak but showed no window in
  40 s, and YouTube raising is not yet verified; both remain UNVERIFIED.
- **Phase 8:** `tesseract` is not installed, so the live OCR layer is UNVERIFIED. The
  parser, screenshot fallback and OCR-coordinate click are unit-tested.
- **Phase 8:** `small.en` measured **1.75–2.08 s** per saved clip (not the requested
  0.55–0.75 s) and regressed one clip (`mute unmute` → `Mute on mute.`, rejected by the
  repetition guard). `WHISPER_MODEL=base.en` remains the faster fallback.
- **Phase 8:** `load_config` gives exported environment variables precedence over `.env`
  (existing design). If `env | grep WHISPER` shows old values, `unset` them before a live
  small.en test.
- **Phase 8:** continuous watching intentionally does not screenshot; OCR runs only for an
  explicit screen read/click request.
- The prompt now exposes every registered tool. The static prefix is cached once, but the
  first call after a server restart still pays the full prompt evaluation.
- `press_key` is limited to navigation keys; it deliberately cannot inject arbitrary text or
  destructive shortcuts.
- Gemini rollback is kept but not exercised in local tests beyond `test_no_gemini.py` and the
  pinned cloud tests.
- Nothing has been pushed to a remote.

---

## 9. Future roadmap (ideas, not commitments)

1. **Wake-word robustness:** compare `small.en` on more of the user's own clips; tune the
   dynamic hotword set; train a tiny personal wake model; add a push-to-talk hotkey as a
   guaranteed fallback. Phase 8 switched the default to small.en and measured the latency.
2. **Skill library:** ship a curated set of safe skills (coding mode, meeting mode, focus
   mode), version them, and let the user export/import.
3. **Experience-driven tuning:** use `training_dataset.jsonl` to mine frequent phrasings,
   add them to the fast path, and (only with explicit approval) fine-tune a small adapter.
4. **Screen understanding:** **Phase 8 delivered this** — AT-SPI `@ref` snapshots,
   semantic/coordinate clicks, OCR fallback and a 1.2 s watcher. Next: install tesseract,
   verify the browser tier live, and add a visible “eyes paused” control.
5. **Browser automation:** use a real tab-control mechanism if one becomes available;
   otherwise keep the honest “opened the search page” fallback. Phase 8's
   `--force-renderer-accessibility` exposes web buttons to AT-SPI.
6. **Latency:** Phase 7 caches the static prefix (`id_slot:0`, `cache_prompt:true`). Next:
   measure real llama-server prompt-token reuse and consider dropping tool schemas on the
   final answer call; Phase 8 measured small.en CPU latency honestly.
7. **Safety:** expand secret-path tests, add a visible audit summary command, and consider
   per-app confirmation policies.

---

## 10. Runbook

```sh
cd ~/Downloads/ULTRON

# Tests (never touches mic/speakers/GUI/AT-SPI/screenshots)
.venv/bin/python -m pytest -q

# Benchmark (mocked OS/LLM)
.venv/bin/python main.py benchmark

# Live chat / hands-free
.venv/bin/python main.py chat
.venv/bin/python main.py listen

# Screen eyes / hands (also reachable by voice)
# "what is on my screen", "click on Show more", "expand the show more section",
# "type f1 highlights in the search box"

# Diagnostics
.venv/bin/python main.py doctor
.venv/bin/python main.py voice-test

# Phase 8 probes
/usr/bin/python3 -c "import gi; gi.require_version('Atspi','2.0'); from gi.repository import Atspi; print('Atspi OK')"
agent-desktop list-windows
command -v tesseract   # optional OCR layer; install with sudo dnf install tesseract

# Skills and dataset export
.venv/bin/python main.py teach "work mode" "open code; open github"
.venv/bin/python main.py import-skills /path/to/skills.json
.venv/bin/python main.py export-dataset
# -> workspace/ultron_finetune_chatml.jsonl (ChatML messages for Unsloth/QLoRA)

# Leave no processes behind
ss -ltnp | grep 9931
pgrep -a ydotoold
pgrep -af atspi_helper.py
```

---

## 11. File map (where to look first)

| Question | File |
|---|---|
| How does a command flow? | `assistant/brain/agent_loop.py`, `fast_path.py` |
| Why did Ultron say that? | `assistant/brain/tool_replies.py`, `reply_style.py`, `tool_output.py` |
| How is the prompt built? | `assistant/brain/prompts.py` |
| How does the mic decide speech ended? | `assistant/speech/recorder.py`, `vad.py`, `interfaces/listen.py` |
| How does STT work? | `assistant/speech/ears.py` |
| How does the wake word work? | `assistant/speech/wake.py` |
| How do the eyes work? | `assistant/vision/screen_watcher.py`, `atspi_helper.py` |
| How do clicks/typing work? | `assistant/tools/screen_actions.py`, `desktop_control.py` |
| How are apps launched? | `assistant/tools/apps.py` (`gio launch`, aliases) |
| How does TTS work? | `assistant/speech/mouth.py`, `interfaces/voice.py` |
| How are tools declared/guarded? | `assistant/tools/registry.py`, `safety/permissions.py` |
| Where are settings? | `assistant/config.py`, `.env.example`, `config/settings.yaml` |
| Where is the history? | `docs/PROGRESS.md`, this file |
| How do I resume? | §12 below |

---

## 12. How to resume in a new session

1. Read this file, then `docs/PROGRESS.md` (top section), then
   `docs/ULTRON_LOCAL_HANDOFF.md`, then `AGENTS.md`.
2. `git branch --show-current` and `git log --oneline -5`. Expected branch for Phase 8:
   `phase8-eyes-hands-ears` (created from `phase7-llm-brain-wayland` @ `242a57f`).
3. `.venv/bin/python -m pytest -q` and compare with §7 (**537 passed** expected).
4. Read the current phase section in `docs/PROGRESS.md`; continue from its **NEXT STEP**.
5. Do one part at a time. Update this file and `docs/PROGRESS.md` before/after each part.
   Commit locally by file name only. Never push, never commit secrets.
6. Stop any `llama-server`, `ydotoold` or `atspi_helper.py` you started; verify with
   `ss -ltnp | grep 9931`, `pgrep -a ydotoold` and `pgrep -af atspi_helper.py`.

---

## 13. Changelog of this document

- **2026-11-03:** created at Phase 6 start. Captures Phases 0–5 B6, the failure catalogue,
  the Phase 6 plan, the verification ledger, limitations, roadmap, runbook, and resume
  procedure.
- **2026-11-03 (end):** Phase 6 verified: 450 tests pass, benchmark 25/25, real saved-clip
  prompt comparison recorded, screen/CTA caveat recorded, skills CLI verified, and the
  failure catalogue updated with the AGC, in-tab search, history and reply fixes.
- **2026-11-03 (Phase 7):** Wayland 3-layer screen sensor, static KV cache prefix with
  `id_slot:0`/`cache_prompt:true`, all-tool exposure, 220-token cap and sentence guard,
  unified fast-path/LLM history, clipboard typing up to 4000 chars, timers, tab-close
  routing, alias/fact memory, ChatML export, and the Flatpak/Electron EPIPE fix. 500 tests
  pass; benchmark 25/25 at 1.559 ms (mocked); live hardware items stay UNVERIFIED.
- **2026-11-05 (Phase 8):** AT-SPI2 eyes (`atspi_helper.py`, `ScreenWatcher`, `@ref`
  snapshots with boxes/actions), optional agent-desktop adapter, OCR fallback,
  `click_element`/`read_screen`/`focus_and_type` with three click tiers, small.en ears with
  beam 2 + dynamic screen hotwords, universal `gio launch` + Fedora aliases, 37 tests,
  full suite **540 passed**, benchmark **25/25** at 1.480 ms. Measured small.en latency
  1.75–2.08 s/clip (not the 0.55–0.75 s target); live clicks/typing/OCR stay UNVERIFIED.
  GNOME 50.5 introspection key absence recorded as failure #24.
- **2026-11-05 (Phase 8 live GUI):** real `gio launch` + AT-SPI + `close_app` verified for
  calculator, text editor, files, terminal and settings; Lutris starts as a Flatpak but shows
  no window. Fixed `close_app` for the 15-character Linux `comm` truncation and for the
  DEVNULL `flatpak ps` capture (failures #27–#28). Three new tests; full suite **540 passed**;
  benchmark **25/25** at 1.480 ms (mocked).
