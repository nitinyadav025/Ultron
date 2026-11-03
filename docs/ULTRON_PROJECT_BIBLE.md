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
| Folder | `~/Downloads/assistant-project` |
| Python | `.venv/bin/python` (3.14.8) — never the system `python3` |
| Brain | LFM3-1.7B GGUF on llama.cpp `llama-server`, OpenAI-compatible API `http://127.0.0.1:9931/v1` |
| Ears | faster-whisper `base.en`, CPU, int8, float32 numpy arrays only |
| Mouth | Piper `en_US-lessac-medium`, CPU, streaming sentence by sentence |
| GPU | GTX 1650 4 GB, reserved for the LLM only |
| Default backend | `BACKEND=local` (no API key needed) |
| Rollback | `BACKEND=gemini` must keep working |
| Branch (Phase 6) | `phase6-intelligent-agent` (created from `phase5-b6-smart-brain` @ `8e2646f`) |
| Test state at Phase 6 start | **415 passed, 0 failed, 0 errors** (`pytest -q`, 46.79 s) |
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
| Tools | `assistant/tools/registry.py`, `files.py`, `apps.py`, `volume.py`, `web.py`, `desktop_control.py`, `sandbox.py` | Real OS actions plus schemas and permission gate |
| Speech | `assistant/speech/recorder.py`, `vad.py`, `ears.py`, `mouth.py`, `wake.py`, `mic_input.py`, `denoise.py` | Capture, endpointing, STT, TTS, wake matching, mic gain |
| Interfaces | `assistant/interfaces/cli.py`, `gui.py`, `listen.py`, `voice.py`, `wake_train.py` | Chat loop, hands-free loop, voice controller |
| Safety | `assistant/safety/permissions.py`, `killswitch.py`, `audit.py`, `redact.py` | Permission tiers, kill switch, audit log, secret redaction |
| Tests | `tests/` | 415 deterministic tests at Phase 6 start |

### 3.3 Prompt discipline

The local prompt is a short static header plus a trailing `Ctx:` line. Static text stays
byte-identical so llama-server can reuse its small cache. Phase 5 reduced the first-call
prompt from **599 to 455 tokens** and the post-`list_files` call from **5 756 to 548 tokens**
(−90 %). Phase 6 adds one compact `Screen:` line and caps history at three turns.

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

## 7. Verification ledger (Phase 6)

> Every number here must come from an actual command run in this session. Anything not run
> is labelled UNVERIFIED.

| Check | Command | Result |
|---|---|---|
| Permission file at HEAD | `.venv/bin/python -m pytest tests/test_permission_modes.py -v` | **12 passed** |
| Permission file after Phase 6 | rerun in `tests/test_phase6...` subprocess | **12 passed, 0 errors** |
| Full suite at HEAD | `.venv/bin/python -m pytest -q` | **415 passed** (46.79 s) |
| Full suite after Phase 6 | `.venv/bin/python -m pytest -q` | **450 passed** (39.19 s) |
| Phase 6 tests | `tests/test_phase6_intelligent_agent.py` | **31 passed** |
| Benchmark | `.venv/bin/python main.py benchmark` | **25/25 passed**, avg **2.063 ms** (mocked) |
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
- Gemini rollback is kept but not exercised in local tests beyond `test_no_gemini.py` and the
  pinned cloud tests.
- Nothing has been pushed to a remote.

---

## 9. Future roadmap (ideas, not commitments)

1. **Wake-word robustness:** compare `small.en` offline (only if the user approves the
   download); train a tiny personal wake model; add a push-to-talk hotkey as a guaranteed
   fallback.
2. **Skill library:** ship a curated set of safe skills (coding mode, meeting mode, focus
   mode), version them, and let the user export/import.
3. **Experience-driven tuning:** use `training_dataset.jsonl` to mine frequent phrasings,
   add them to the fast path, and (only with explicit approval) fine-tune a small adapter.
4. **Screen understanding:** optionally read the active window title and selected text via
   AT-SPI; never screenshot without consent.
5. **Browser automation:** use a real tab-control mechanism if one becomes available;
   otherwise keep the honest “opened the search page” fallback.
6. **Latency:** measure prompt tokens per turn, consider dropping tool schemas on the final
   answer call, and cache the static prompt prefix.
7. **Safety:** expand secret-path tests, add a visible audit summary command, and consider
   per-app confirmation policies.

---

## 10. Runbook

```sh
cd ~/Downloads/assistant-project

# Tests (never touches mic/speakers/GUI)
.venv/bin/python -m pytest -q

# Benchmark (mocked OS/LLM)
.venv/bin/python main.py benchmark

# Live chat / hands-free
.venv/bin/python main.py chat
.venv/bin/python main.py listen

# Diagnostics
.venv/bin/python main.py doctor
.venv/bin/python main.py voice-test

# Skills
.venv/bin/python main.py teach "work mode" "open code; open github"
.venv/bin/python main.py import-skills /path/to/skills.json

# Leave no processes behind
ss -ltnp | grep 9931
pgrep -a ydotoold
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
| How does TTS work? | `assistant/speech/mouth.py`, `interfaces/voice.py` |
| How are tools declared/guarded? | `assistant/tools/registry.py`, `safety/permissions.py` |
| Where are settings? | `assistant/config.py`, `.env.example`, `config/settings.yaml` |
| Where is the history? | `docs/PROGRESS.md`, this file |
| How do I resume? | §12 below |

---

## 12. How to resume in a new session

1. Read this file, then `docs/PROGRESS.md` (top section), then
   `docs/ULTRON_LOCAL_HANDOFF.md`, then `AGENTS.md`.
2. `git branch --show-current` and `git log --oneline -5`. Expected branch for Phase 6:
   `phase6-intelligent-agent`.
3. `.venv/bin/python -m pytest -q` and compare with §7.
4. Read the current phase section in `docs/PROGRESS.md`; continue from its **NEXT STEP**.
5. Do one part at a time. Update this file and `docs/PROGRESS.md` before/after each part.
   Commit locally by file name only. Never push, never commit secrets.
6. Stop any `llama-server` or `ydotoold` you started; verify with
   `ss -ltnp | grep 9931` and `pgrep -a ydotoold`.

---

## 13. Changelog of this document

- **2026-11-03:** created at Phase 6 start. Captures Phases 0–5 B6, the failure catalogue,
  the Phase 6 plan, the verification ledger, limitations, roadmap, runbook, and resume
  procedure.
- **2026-11-03 (end):** Phase 6 verified: 450 tests pass, benchmark 25/25, real saved-clip
  prompt comparison recorded, screen/CTA caveat recorded, skills CLI verified, and the
  failure catalogue updated with the AGC, in-tab search, history and reply fixes.
