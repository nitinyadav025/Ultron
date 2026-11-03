# Ultron progress — Phase 6 (current)

Last updated: 2026-11-03 (Phase 6 code, tests and docs complete; live microphone test pending)

> **Read `docs/ULTRON_PROJECT_BIBLE.md` first.** It is the single, detailed entry point:
> architecture, every phase, the live-failure catalogue with root causes/fixes, the Phase 6
> result, limitations, roadmap, runbook and the exact resume procedure.

## Phase 6 - Screen-Aware LLM Brain, In-Tab Search, AGC Guard & Skill Training (Nov 3)

Work branch: `phase6-intelligent-agent` (from `phase5-b6-smart-brain` @ `8e2646f`, local
only, nothing pushed).

| Part | What | Status |
|------|------|--------|
| 1 | Audit/fix the 9 reported permission-test errors | DONE (not reproducible; 12/12 and full suite 0 errors) |
| 2 | Dynamic mic AGC guard + Whisper lexical priming + phonetic repairs | DONE |
| 3 | `wmctrl -l` screen awareness, Google default, site/in-tab search | DONE |
| 4 | LLM-first brain, 3-turn history cap, screen header, professional replies | DONE |
| 5 | Trainable skills + experience dataset | DONE |
| 6 | `tests/test_phase6_intelligent_agent.py`, full pytest, benchmark | DONE (450 passed, 25/25) |
| 7 | Docs, final verification, local commits | DONE (see head state) |

### Part 1 — the 9 permission errors

- User report: `.venv/bin/python -m pytest -q` → `415 passed, 9 errors` in
  `tests/test_permission_modes.py`, including `test_confirm_tier_tools_still_ask` and
  `test_untrusted_content_still_forces_confirmation`.
- Re-checked at `8e2646f` on Nov 3:
  - `tests/test_permission_modes.py -v` → **12 passed in 0.72 s**.
  - full `pytest -q` → **415 passed in 46.79 s**.
- The file never reads stdin: every gate in it receives an explicit `confirm_callback`.
  No permission check was weakened. The report is kept in the failure catalogue; no
  reproducible cause was found at HEAD.
- Phase 6 adds a subprocess guard test: `tests/test_phase6_intelligent_agent.py` reruns
  the permission module and asserts **12 passed, 0 errors**.

### Part 2 — AGC guard, lexical prompt, phonetic repair

- `assistant/speech/mic_input.py`:
  - `AGC_PEAK_THRESHOLD=32700`, `AGC_RMS_THRESHOLD=3500`.
  - `agc_guard_needed(peak, rms)` matches exactly the Nov 3 Brave signature.
  - `guard_mic_agc()` restores `MIC_SOURCE_VOLUME` or **0.21** in a daemon thread.
  - `schedule_mic_volume_restore()` re-applies the safe level 1.5 s after a browser launch.
  - Tests can inject a runner; `PYTEST_CURRENT_TEST`/`ULTRON_NO_MIC` stop any real `wpctl`.
- Wired into `VoiceController._finish_recording` (`/voice`, voice-test) and
  `HandsFreeListener._process` (listen). Browser hooks are in `web.open_url` and
  `apps._launch_match`.
- Whisper: `WHISPER_INITIAL_PROMPT` default is a **153-character natural sentence**
  (`Ultron, Hey Ultron. Commands: … F1, scroll down, new tab, close tab, type.`), passed as
  `initial_prompt`; an explicit empty value disables it. Hotwords stay opt-in.
- Real saved-clip comparison (base.en, CPU int8, 2026-11-03):
  - 8 valid command clips: **8/8 semantically correct with and without the prompt**
    (the prompt changed punctuation/capitalisation on one clip, not meaning).
  - 8 saved single-“Ultron” clips: **6/8 wake matches without prompt → 8/8 with prompt**
    (the two previously rejected clips became “Ultron”).
  - 4 saved “Hey Ultron” clips: **2/4 both ways**; the prompt did not fix those two.
  - No word-list echo returned; longest prompt-enabled wake transcript < 120 chars.
- `wake.py`: added risky aliases `hey everyone`, `iron`, `here drawn`, `altron`,
  `all drawn`; they only wake Ultron when followed by a real fast-path command (or alone).
  Phonetic repairs: `third/hurt/sir youtube` → `search youtube` (with a query:
  `search <q> in youtube`), and `right <text>` → `write <text>`.

### Part 3 — screen awareness and in-tab search

- `assistant/brain/context.py`: `SESSION.open_windows`, `active_site`, `last_site`,
  `read_window_titles()` (`wmctrl -l`, 0.2 s) and `screen_line()`.
  `get_screen_context()` refreshes browser/site from real titles.
- Live check on this machine: `wmctrl -m` → GNOME Shell, exit 0; `wmctrl -l` returned no
  titles at check time because no GUI windows were open. The parser is covered by
  deterministic tests.
- `web.py`:
  - Google is the default (`SEARCH_ENGINE=google`); `.env.example` updated and
    `SEARCH_ENGINE=google` appended to `.env`.
  - `web_search(query, browser=None, site=None, in_current_tab=False)` supports YouTube,
    GitHub, Reddit, Wikipedia, Google, Amazon, Spotify, Gmail URLs.
  - “search f1 in youtube” → `web_search(query="f1", site="youtube")` with
    `https://www.youtube.com/results?search_query=f1`.
  - “search f1” while YouTube is the active site → the same URL in the **current tab**.
  - In-tab navigation: focus with `wmctrl -a`, Ctrl+L, `wl-copy`, Ctrl+V, Enter via
    `ydotool`; falls back to opening the URL if injection is unavailable.
  - `open_first_result` never uses `btnI=1`: known brands open their canonical site,
    YouTube can open the first parsed video, other queries use a clean first-result fetch
    or the Google results page.
  - Fast path supports “play/click/open first video” (needs a previous query).
- Prompt/tool schema: `web_search` now exposes `site` and `in_current_tab`; the compact
  schema stays under the measured token cap.

### Part 4 — LLM-first brain, flat history, professional replies

- `AgentLoop.run_task` now wraps `_run_task`:
  - prunes before each turn to 5 messages, then the new user message keeps the prompt at
    ≤6;
  - prunes after every turn to **3 text-only turns / 6 messages**, retaining only the
    current turn’s tool protocol when it is still needed.
- Measured with a deterministic stub (JSON chars / 4 estimate, not llama-server tokens):
  chat prompt **Turn 1 = 375 chars (~94 tokens), Turn 15 = 580 chars (~145 tokens)** and
  identical from Turn 4 onward (turns 14 and 15 are byte-identical). A tool turn adds the
  constant compact schema: **1,967 chars (~492 estimated tokens)**. Actual
  `llama-server` prompt-token counts are UNVERIFIED in this offline run.
- Every local LLM call receives one compact `Screen: browser=…, site=…, windows=[…]` line.
- `prompts.py` tells the model that input is STT, that screen state is live, and that
  `learn_skill` exists. Local prompt length: **784 characters** (under the 800 guard).
- `tool_replies.py` speaks natural professional sentences, e.g.
  “I’ve opened Firefox for you.”, “Searching YouTube for F1 right now.”,
  “I’ve closed that tab.”, “I’ve scrolled the page down.”,
  “I’ve typed that into the active window.”

### Part 5 — skills and experience dataset

- New `assistant/brain/skills.py`, backed by `config/learned_skills.json` and
  `workspace/learned_skills.json` (workspace wins).
- Voice forms: “remember when I say …, …”, “learn skill … means …”, “when I say …, …”.
- CLI: `.venv/bin/python main.py teach "<trigger>" "<action1>; <action2>"` and
  `.venv/bin/python main.py import-skills <path.json>` (verified in a temp workspace).
- `AgentLoop` matches a skill by exact/prefix/keyword/fuzzy score ≥0.80, executes the fast
  path action chain, and otherwise injects the top two skills as a compact `Skills:` hint.
- `learn_skill` tool registered in CLI, GUI and listen.
- `workspace/training_dataset.jsonl` logs only accepted wake commands:
  timestamp, raw/repaired transcript, screen context, tools, reply and latency. No audio,
  no secrets, no ignored speech.

### Part 6 — verification

- `.venv/bin/python -m pytest -q` → **450 passed, 0 failed, 0 errors** (39.19 s).
- `.venv/bin/python main.py benchmark` → **25/25 passed**, average **2.063 ms** (mocked;
  live mic/LLM latency is UNVERIFIED).
- New `tests/test_phase6_intelligent_agent.py` (31 tests) covers the 9 permission tests,
  AGC, lexical prompt, screen/site/in-tab search, phonetic repairs, wake aliases, 15-turn
  history cap, skills and the dataset.
- `compileall` OK; `git diff --check` clean.

### Files changed (Phase 6)

- New: `assistant/brain/skills.py`, `config/learned_skills.json`,
  `tests/test_phase6_intelligent_agent.py`, `docs/ULTRON_PROJECT_BIBLE.md`.
- Speech: `assistant/speech/mic_input.py`, `assistant/speech/ears.py`,
  `assistant/speech/wake.py`, `assistant/interfaces/voice.py`,
  `assistant/interfaces/listen.py`.
- Brain/tools: `assistant/brain/context.py`, `assistant/brain/fast_path.py`,
  `assistant/brain/agent_loop.py`, `assistant/brain/prompts.py`,
  `assistant/brain/tool_replies.py`, `assistant/tools/web.py`,
  `assistant/tools/apps.py`, `assistant/tools/desktop_control.py`,
  `assistant/tools/registry.py`, `assistant/config.py`, `main.py`,
  `assistant/interfaces/cli.py`, `assistant/interfaces/gui.py`.
- Tests updated: `test_ears.py`, `test_fast_path.py`, `test_web.py`,
  `test_b4_live_bugs.py`, `test_b5_full_control.py`, `test_b6_smart_brain.py`,
  `test_reply_style.py`, `scripts/benchmark_phase5c.py`.
- Config/docs: `.env.example`, `.env` (SEARCH_ENGINE only, not committed), `AGENTS.md`,
  `docs/PROGRESS.md`, `docs/ULTRON_PROJECT_BIBLE.md`.

### NEXT STEP — 5-step live test (the coding tool cannot use the mic/speakers)

Run from `~/Downloads/assistant-project`:

1. `.venv/bin/python main.py listen` — say **“Ultron, open YouTube”**, then
   **“search f1 in youtube”**. Expected: one tab navigates to
   `https://www.youtube.com/results?search_query=f1`; Ultron says
   “Searching YouTube for f1 right now.”
2. With YouTube already open, say **“Ultron, search f1”**. Expected: the same tab searches
   YouTube (no duplicate tab), not Google.
3. Say **“Ultron, Hey everyone open firefox”** and **“Ultron, Iron search for youtube”**.
   Expected: Firefox opens; the second searches YouTube. Say **“Third YouTube”** after the
   wake word: same result.
4. Open Brave, then speak a command at normal volume and check the next utterance:
   `logs/assistant.log` must show `Mic AGC guard: restoring @DEFAULT_AUDIO_SOURCE@ to 0.21`
   if Brave boosted the source; peak should return near the old level.
5. Say **“Ultron, remember when I say work mode, open code and open github”**, then
   **“Ultron, work mode”**. Expected: “I’ve learned …” and both apps open. Check
   `workspace/training_dataset.jsonl` has the accepted command with tools and reply.

### Head state (Phase 6)

- Branch `phase6-intelligent-agent` (from `phase5-b6-smart-brain` @ `8e2646f`), local only,
  **nothing pushed**.
- Commits:
  - `07781dc` — P6 parts 1–2: AGC guard, lexical prompt, phonetic repairs
  - `2e0bc2a` — P6 part 3: screen context and in-tab site search
  - `16a8448` — P6 parts 4–5: flat history, natural replies, skills
  - `a970252` — P6 part 6: Phase 6 suite and benchmark expectations
  - `8350d0e` — P6 part 7: project bible and progress checkpoint
- Final pytest: **450 passed, 0 failed, 0 errors**; benchmark **25/25**, average
  **2.063 ms** (mocked).
- `ss -ltnp | grep 9931` → **empty** (no llama-server).
- `pgrep -a ydotoold` → **empty** (no keyboard daemon).
- Working tree clean; `.env` (only the new `SEARCH_ENGINE=google` line) was not staged.

---

## Phase 5 B6 - Neural VAD Endpointing, 1s LLM Brain & Smart Control (Nov 2)

Work branch: `phase5-b6-smart-brain` (from `phase5-b5-full-control` @ `ff266f8`,
local only, nothing pushed). This section supersedes every section below it.

| Part | What | Status |
|------|------|--------|
| 1 | Silero VAD + adaptive 850 ms endpointing, 12 s cap | DONE |
| 2 | Wake aliases, error-loop guard, prefix stripping | DONE |
| 3 | One LLM call per tool turn + compact schemas + compounds | DONE |
| 4 | Natural tabs, web-app fallback, fast scroll/type | DONE |
| 5 | `tests/test_b6_smart_brain.py` for every live failure | DONE (53 tests) |
| 6 | Full pytest + benchmark | DONE (415 passed, 25/25) |
| 7 | Docs + local commits | DONE |

### Part 1 — Silero VAD + adaptive endpointing (DONE)

- New `assistant/speech/vad.py` wraps `faster_whisper.vad.get_speech_timestamps`
  (already bundled in `.venv`; verified import). It lazily loads the ONNX model once,
  is thread-safe, and returns `None` if the model fails so the energy rules still run.
- `record_until_silence` and `UtteranceDetector` now use:
  - a rolling background floor (last 3 s of non-speech frames, 10th percentile), so a
    fan at RMS 1700 raises `noise_floor` instead of leaving it stuck at 85;
  - Silero-confirmed start when VAD is available (a fan cannot start an utterance);
  - a trailing 900 ms VAD check every ~150 ms, evaluated over the last 850 ms;
  - end at 850 ms of quiet (energy < 45 % of speech or within 6 dB of the floor, or VAD
    hears 0 ms), with a 1.2 s VAD-silent backstop;
  - recorder default `silence_ms` 850 ms, hands-free final `VOICE_SILENCE_MS` 1350 ms,
    `VOICE_MAX_SECONDS` 12 s, `LISTEN_EARLY_CHECK_MS` 850 ms.
- End threshold is capped at 50 % of the speaker's active level; the post-floor rule
  never calls a frame near the speaker's own level silent. Without this the first replay
  cut the 2400 speech when the fan raised the floor to 1700.
- Replay (500 ms floor 100 + 2.0 s speech RMS 2400 + 10 s fan RMS 1700):
  recorder `reason=silence` at **3360 ms**, i.e. **860 ms after speech ends**, never
  `max_length`; the detector's early event is at the same 860 ms. A 3 s fan with VAD
  hearing no speech raises the floor from 85 to **1622**.
- Production wiring: `run_listen`, `run_cli_chat`, `VoiceController` and `wake-train`
  pass the shared Silero wrapper. Unit tests leave it `None`, so synthetic tones still
  use the deterministic energy rules.

### Part 2 — wake aliases, error loop, prefix stripping (DONE)

- `listen.py` never passes `verdict != ok`, empty text, or `"I didn't hear anything."`
  to `wake.py`/`fast_path`. The turn logs `result=no_real_word`, prints
  `· heard noise, ignored`, resets the detector and leaves the follow-up window
  untouched. `test_no_real_word_never_reaches_wake_match` monkeypatches `wake_match` to
  raise, so the old 0.86 `"i didn"` match is impossible.
- `workspace/wake_aliases.json` was cleaned with the new filter (all 14 polluted prefixes
  removed; the file now has `"aliases": {}`). `wake_train.py` only records prefixes when
  the plain decode is `ok`, and `wake.py:is_safe_learned_alias` rejects
  `<rejected...>`, `i`, `i didn`, `i didn t`, `i ll`, `i m`, `what do`, `what do you`,
  `hey all`, and contraction fragments. `load_learned_aliases` re-filters old files.
- `wake.py` adds `i'll turn`, `ill turn`, `here ultron`, `here drone`, `hailtone`,
  `hey ultron`; every match strips its prefix plus fillers (`an, and, to, please, then,
  so, can you`). `"Hey Ultron, Hey Ultron, Hey Ultron"` strips to `""` -> `"Yes?"`.
- `ears.py:looks_like_repetition_loop` removes `ultron`/`hey` before the repeat check,
  so repeated wake words are not a decoder loop.
- A mid-sentence wake word (`"Open Steam, Ultron can you open Discord for me?"`) is handled
  by `wake_match_anywhere`, which joins both sides with `and`; the listener accepts it
  only when the result is a fast-path sentence.

### Part 3 — one LLM call, compact schemas, compound fast path (DONE)

- `agent_loop.py` collects every executed tool. If every result has a deterministic
  `code_reply_for_tool`, it appends the joined reply to history and returns **without a
  second llama-server call**. Two tools join as `"Steam is open. Discord is open."`; a tool
  with no code builder (`read_file`) still gets the second call.
- `registry.py` compact mode: 8 tools (`open_app, close_app, web_search,
  open_first_result, type_text, scroll_page, set_volume, mute_audio`), short descriptions,
  required params plus the few optional ones the model uses. Measured
  `len(json.dumps(tools))/4 = 370` estimated tokens (1482 chars), under the ~380 target.
  The default (Gemini/tests) still returns all 19 tools and all parameters.
- `prompts.py` adds four natural-language tool examples and the "two actions -> call
  every tool" rule; the local prompt is 737 characters.
- `fast_path.py` supports compounds joined by `and`/`then`, multiple sentences, and the
  comma-wake split. Examples: `open steam and open discord`,
  `open a new tab and search for github`, `switch to firefox and scroll down`,
  `open firefox and list my downloads`.

### Part 4 — natural tabs, web apps, fast scroll/type (DONE)

- Tab phrasings: `go to the new tab`, `open the tab`, `open a new tab in <browser>`,
  `new tab in <browser>` (focus the browser first), `okay, close the tab`,
  `close the tab <name>`, `shut this tab`, `reopen the tab`, `restore tab`.
- `Search, Instagram` -> `web_search(instagram)`; `Open the first link, please.` uses
  `SESSION.last_query`; `Can you open the first link of GitHub?` -> github.
- `apps.py` web fallback: Discord, Instagram, YouTube, WhatsApp, ChatGPT, Gmail, GitHub,
  Steam, Netflix, Spotify, Reddit, Twitter/X, Google, Claude. Local `.desktop`/apps.yaml
  wins; otherwise the URL opens in `choose_browser()` and the reply is
  `"Opening <Name> in <Browser>."`.
- `desktop_control.py` starts one `ydotoold --socket-path=/tmp/.ultron_ydotool.sock` for
  the session (`start_ydotool()` at chat/listen startup; atexit stop). Tests never start
  it (`PYTEST_CURRENT_TEST` guard). `scroll_page` sends PageDown/PageUp (109/104), so
  one `"scroll down"` moves a full screen and never prints `^[[B^[[B...`. Before scroll,
  tab or type, `wmctrl -a <last app/browser>` runs (when the terminal context is on) and
  waits 80 ms; an explicit "in the terminal" request skips the focus. `type_text` copies
  via `wl-copy` and sends `ydotool type --key-delay 12 -- <text>`.

### Part 5 — tests (DONE)

`tests/test_b6_smart_brain.py` (53 tests) proves:
1. VAD replay (85 floor, 2400 speech, 1700 fan) stops in 860 ms, never `max_length`;
   the rolling floor reaches 1622 on a fan; the hard cap is 12 s; real Silero reads a
   saved speech clip and says the saved 30 s fan is not speech.
2. `"I didn't hear anything."` (`no_real_word`) never calls `wake_match`, never speaks and
   never extends the follow-up window, even with `"i didn"` in the learned list.
3. `"Hey Ultron, Hey Ultron, Hey Ultron"` is not `repetition_loop` and answers `"Yes?"`.
4. `"I'll turn open VS code"` strips to `open vs code` and routes to `open_app(code)`.
5. Every Nov 2 phrase: Firefox+GitHub search, first link (please/of), `Search, Instagram`,
   new/close/reopen tab, `Open Steam, Ultron ... Discord`, scroll down.
6. `agent_loop` calls the model once for `open_app`/multi-`open_app`, twice for
   `read_file`; the compact schema is <= 380 estimated tokens.
7. Web-app URLs for all 14 names, local install wins, daemon start-once, PageDown,
   `--key-delay 12`, `wmctrl` focus, terminal skip.

### Part 6 — verification (DONE)

- `.venv/bin/python -m pytest -q` → **415 passed, 0 failed** (37.7 s).
- `.venv/bin/python main.py benchmark` → **25/25 passed**, average **1.403 ms** (mocked;
  live mic/LLM latency is UNVERIFIED).
- `compileall` OK; `git diff --check` clean.

### Part 7 — head state (DONE)

- Branch `phase5-b6-smart-brain` (from `phase5-b5-full-control` @ `ff266f8`), local only,
  nothing pushed.
- No `llama-server` on port 9931 and no `ydotoold` process or `/tmp/.ultron_ydotool.sock`
  left by tests (checked below).
- `.env`, `models/`, `workspace/`, `logs/`, `.backups/` and `.venv/` are not staged.

### Files changed (B6)

- New: `assistant/speech/vad.py`, `tests/test_b6_smart_brain.py`.
- Speech/listening: `assistant/speech/recorder.py`, `assistant/speech/wake.py`,
  `assistant/speech/ears.py`, `assistant/interfaces/listen.py`,
  `assistant/interfaces/voice.py`, `assistant/interfaces/wake_train.py`.
- Brain/tools: `assistant/brain/agent_loop.py`, `assistant/brain/fast_path.py`,
  `assistant/brain/prompts.py`, `assistant/brain/tool_replies.py`,
  `assistant/tools/registry.py`, `assistant/tools/apps.py`,
  `assistant/tools/desktop_control.py`, `assistant/interfaces/cli.py`,
  `assistant/config.py`, `.env.example`, `scripts/benchmark_phase5c.py`.
- Tests updated: `tests/test_b4_live_bugs.py`, `tests/test_listen.py`,
  `tests/test_config_defaults.py`, `tests/test_fast_path.py`,
  `tests/test_b5_full_control.py`, `tests/test_reply_style.py`,
  `tests/test_file_paths.py`.
- Docs: `docs/PROGRESS.md`.

### What changed for the user's Nov 2 phrases

- `"I'll turn open VS code"` -> wake prefix stripped, `open_app("code")`, `"Code is open."`.
- `"Open the first link, please."` -> `open_first_result(SESSION.last_query)`, reply
  `"Opening the first result for github."`.
- `"Search, Instagram"` -> `web_search("instagram")`, `"Searching for instagram in Firefox."`.
- `"Go to the new tab"` -> `browser_tab_control("new_tab")`, `"Opened a new tab."`.
- `"Close the tab Instagram"` -> `browser_tab_control("close_tab")`, `"Closed the tab."`.
- `"Open Steam, Ultron can you open Discord for me?"` -> two `open_app` calls; each uses
  the local app if installed, else the web fallback.
- `"Scroll down."` -> one PageDown after focusing the last browser; no `^[[B`.

### NEXT STEP — 5-step live test (the coding tool cannot use the mic/speakers)

Run from `~/Downloads/assistant-project`:

1. `.venv/bin/python main.py listen` — say **"Ultron"**, then **"open firefox"**. No
   recording may last 12 s; a fan may stay on.
2. Say **"Scroll down."** with Firefox focused. Expected: one full page, no `^[[B`.
3. Say **"Ultron, open a new tab"** and **"Ultron, open the first link, please."**
   (after a search). Expected: one tab; the first GitHub result.
4. Say **"Ultron, open Steam"** and **"Ultron, open Discord"**. Expected: Steam locally if
   installed; Discord in the browser with `"Opening Discord in Firefox."`.
5. Say **"Ultron, I'm done with Firefox, shut it down."** Expected: `"Closed Firefox."`;
   then check `pgrep -a ydotoold` (one daemon while listen runs) and `ss -ltnp | grep 9931`
   (llama-server only while the app runs).

---

# Ultron progress — Phase 5 B5 + Part C + Full Desktop Control (HISTORY, superseded by B6)

Last updated: 2026-11-01 (B5 + Part C code, tests and benchmark complete)

## Phase 5 B5 + Part C + Full Desktop Control (Nov 1)

Work branch: `phase5-b5-full-control` (from `phase5-b4-work` @ `456ff7f`, local only,
nothing pushed). This section supersedes every section below it.

| Part | What | Status |
|------|------|--------|
| 1 | Smart two-speed endpointing (no mid-sentence cut-offs) | DONE |
| 2 | Browser targeting + session context memory | DONE |
| 3 | Dynamic desktop app discovery (open/close/switch) | DONE |
| 4 | Full laptop control (typing, scroll, tabs, windows, system) | DONE |
| 5 | Fast conversational brain (instant chat + lean local chat) | DONE |
| 6 | Phase 5 Part C 25-command benchmark + pytest | DONE |
| 7 | Full verification, docs, commits | DONE |

**Live evidence (user, Nov 1 20:39–20:41, UNVERIFIED by the coding tool):**

- 7 of 8 `listen` utterances ended `reason=early`; at 20:40:09 one sentence split into
  two `no_wake_word` fragments (3.37 s + 2.35 s). `LISTEN_EARLY_CHECK_MS=600` was too
  aggressive for breathing pauses.
- `"open firefox and search for github"` opened Firefox but searched in **Brave**.
  The real log shows why: at 20:31:10 `Fast path: LLM used for 'Can you open Firefox and
  search for Sadicing?'`, then `open_app({"app_name":"Firefox"})` followed by
  `web_search({"query":"Sadicing"})` with no browser; at 20:28:44 and 20:37:55 a
  standalone search also had `browser=null`, so `xdg-open` used Brave.
- Apps outside `config/apps.yaml` could not be opened or closed.
- `"Hello"` took 2.69 s and `"What is your name?"` 2.24 s
  (`reply_ready=2.69s` and `reply_ready=2.24s` in `logs/assistant.log`).

### Part 1 — smart two-speed endpointing (DONE)

- `LISTEN_EARLY_CHECK_MS` default **1050 ms**, hands-free `VOICE_SILENCE_MS` **1450 ms**,
  `/voice` `VOICE_RECORD_SILENCE_MS` **1350 ms**.
- The early event only finishes a **wake word alone** or a **1–5 word fast-path command**
  that does not end in a continuation word (`and, then, the, a, an, to, for, with, in,
  of, on, my, or, but, also, so, that, because, like, search, open, close, type, write,
  can, you, please, about, from, into, at`).
- Longer speech (>2.5 s or >3.5 s from speech start) skips the early Whisper decode and
  waits for the full 1450 ms window. If the user resumes before 1450 ms, the detector
  continues the same recording; no split.
- Tests:
  - short command early at ~2100 ms (640 ms lead + 400 ms voice + 1050 ms window), final
    only after the extra 400 ms;
  - `"Ultron open firefox" + 1.0 s pause + "and search for github"` → one recording,
    one `transcribe` call, one command;
  - `/voice` 1.0 s pause → one recording;
  - room noise RMS 450 still ends at 1250–1450 ms after speech, never 30 s.

### Part 2 — browser and session context (DONE)

- `choose_browser()` order: explicit browser → `SESSION.last_browser` → Firefox if
  installed → system opener. An explicit or remembered browser always launches that
  exact executable with the URL (`[firefox, url]`), never `xdg-open`.
- `open_app` records `last_app`/`last_browser`; `web_search`/`open_first_result` record
  `last_query`; `close_app` clears the matching `last_app`.
- Follow-ups: `"close it"` closes `last_app`; `"open the first link"` reuses
  `last_query` in `last_browser`.
- Conversational prefixes (`can you`, `could you`, `please`, `I want you to`, `tell me`)
  are stripped before matching, so `"Can you open Firefox and search for Sadicing?"` is
  **one** `web_search(query="sadicing", browser="firefox")` — no LLM, no second action.

### Part 3 — dynamic desktop apps (DONE)

- Cached scanner reads `/usr/share/applications`, `/var/lib/flatpak/exports/share/
  applications`, and `~/.local/share/applications`.
- On this Fedora machine it discovered **176** `.desktop` applications (152 system + 10
  user + 15 Flatpak exports, one duplicate), merged with `config/apps.yaml` as
  high-priority aliases.
- Matching: exact name/key/GenericName/executable, token containment, difflib fuzzy
  score ≥0.86, plus a conservative Soundex phonetic match (for example “fierfox”).
- Garbage such as `"some fake app"` and `"the first link for github"` is rejected with
  `"I couldn't find an app named <name>."`.
- `close_app` derives process names from `Exec=`/`TryExec=`/YAML and keeps the strict
  blocklist (`gnome-shell`, `systemd`, `pipewire`, `wireplumber`, `wayland`, `Xwayland`,
  `bash`, `python`, `sshd`, …). `switch_app` tries `wmctrl -a` then relaunches the
  `.desktop` app to focus it on GNOME Wayland.

### Part 4 — full desktop control (DONE)

Verified without sudo (`getfacl /dev/uinput`, `timeout 2 ydotoold …`, `command -v`):

- `/dev/uinput`: `user:nitinyadav:rw-`; `ydotoold --socket-path=/tmp/…` starts as this
  user and creates the socket (the 2 s timeout killed it; **no daemon was left**).
  Actual key injection into a window is **UNVERIFIED** because automated tests must not
  type into real windows.
- Available: `ydotool`/`ydotoold`, `wl-copy`, `wmctrl`, `gdbus`, `notify-send`,
  `gnome-screenshot`. Missing: `xdotool`, `wtype`, `dotool`, `brightnessctl`,
  `playerctl`.
- GNOME Power DBus on this machine exposes `Keyboard.Brightness` but **not**
  `Screen.Brightness`; `/sys/class/backlight/intel_backlight/brightness` is root-only and
  not writable as the user. `set_brightness` tries the requested GNOME call,
  `brightnessctl`, then sysfs, and returns an honest failure when none is writable.
- New tools: `type_text`, `scroll_page`, `browser_tab_control`, `window_control`,
  `set_brightness`, `media_control`, `system_info`, `take_screenshot`, plus `switch_app`.
  Typing has a 200-character cap, refuses secret-like text, and never presses Enter
  unless `press_enter=True`. Clipboard fallback is reported, never silent.

### Part 5 — fast conversation (DONE)

- Instant, tool-free replies for `hello`, `hi`, `hey`, `hey ultron`, `what is your name`,
  `who are you`, `how are you`, `thank you`, `thanks`, `are you there`.
- Pure conversation that still goes to the local model now sends **no tool schemas**,
  only the last 8 history items (4 turns), a lean chat prompt, and 96 max tokens.
- Live “before”: Hello `reply_ready=2.69 s`, name `reply_ready=2.24 s`. “After” live
  latency is **UNVERIFIED**; the deterministic fast-path router measured 0.007 ms in the
  benchmark and the live transcription stage was 0.34–0.47 s in the old log.

### Part 6 — Phase 5 Part C benchmark (DONE)

- `scripts/benchmark_phase5c.py`, wired as `.venv/bin/python main.py benchmark`.
- 25 realistic commands across apps, web/first link, audio, files, scrolling, tabs,
  typing, windows, system info and conversation.
- Mocked OS runners only: no window opened, no key injected, no screenshot taken, no
  real LLM called.
- Result: **25/25 passed, 0 failed**, average **1.519 ms** (mocked routing/CPU time).
- Report: `logs/benchmark_phase5c.json` (real command) or a temp path during pytest.

### Part 7 — verification (DONE)

- `.venv/bin/python -m pytest -q` → **362 passed, 0 skipped**.
- `.venv/bin/python main.py benchmark` → **25/25**.
- `compileall` → OK; `git diff --check` → clean.
- `ss -ltnp | grep 9931` → no llama-server. `pgrep -a ydotoold` → none.
- The pytest suite’s real-log guard passed (tests use temp logs/workspace).

### Head state (B5)

- Branch `phase5-b5-full-control`, commits `d7903d0` (B5 code), `a3e8fe2`
  (head state) and `7094be3` (final phrases/context polish), local only, nothing pushed.
- Final pytest **362 passed, 0 skipped**; benchmark **25/25** (mocked, average 1.519 ms).
- No llama-server on port 9931; no `ydotoold` left running.
- Working tree clean after the docs commit.

### Files changed

- Part 1: `assistant/config.py`, `.env.example`, `assistant/speech/recorder.py`,
  `assistant/interfaces/listen.py`, `assistant/interfaces/voice.py`.
- Part 2: `assistant/brain/context.py` (new), `assistant/tools/web.py`,
  `assistant/tools/apps.py`, `assistant/brain/fast_path.py`,
  `assistant/interfaces/cli.py`, `assistant/interfaces/listen.py`.
- Part 3: `assistant/tools/apps.py`, `config/apps.yaml`, `assistant/brain/tool_replies.py`.
- Part 4: `assistant/tools/desktop_control.py` (new), `assistant/brain/fast_path.py`,
  `assistant/brain/tool_replies.py`, `assistant/interfaces/cli.py`,
  `assistant/interfaces/listen.py`, `assistant/interfaces/gui.py`.
- Part 5: `assistant/brain/fast_path.py`, `assistant/brain/agent_loop.py`,
  `assistant/brain/prompts.py`.
- Part 6: `scripts/benchmark_phase5c.py` (new), `main.py`,
  `tests/test_phase5c_benchmark.py`.
- Tests: `tests/test_b5_full_control.py` (new) plus updates to existing tests.

**NEXT STEP:** run the live checklist in the final report:
`listen` with “Ultron”, “open firefox”, “open firefox and search for github”,
“close it”, “open the first link”, “scroll down”, “open a new tab”, “type hello world”,
“what is my battery”; then `/voice` with “Hello” and “What is your name?”.

---

# Ultron progress — Phase 5 Part B4

Last updated: 2026-11-01 (B4 code, tests and docs complete; live test pending)

## Phase 5 B4 — Live Bug Fixes & Sub-Second Endpointing (Nov 1)

Work branch: `phase5-b4-work` (from `phase5-b3-work` @ `72a00f0`, local only, nothing
pushed). This section supersedes every section below it.

| Part | What | Status |
|------|------|--------|
| 1 | Sub-second end-of-speech detection (recorder + listen detector) | DONE |
| 2 | Wake word, short hotwords, built-in aliases, `LISTEN_DEBUG` env fix | DONE |
| 3 | `open_first_result` priority, `open_app` validation, real `close_app` | DONE |
| 4 | Speak every `/voice` reply + conversational brain guard | DONE |
| 5 | Deterministic tests for every live bug | DONE |
| 6 | Full pytest, real-log guard, docs | DONE |
| 7 | Local commits on `phase5-b4-work` | DONE |

**Live evidence (user, Nov 1 18:18–18:28, UNVERIFIED by the coding tool):**

- `listen` never saw the wake word and every utterance ran to `duration=30.01s
  reason=max_length` (peak 10795..16459, rms 255..486, noise_floor 84..134).
- In `/voice` the user had to press Enter; auto-stop never fired.
- Post-speech room/breathing RMS 200–450 kept resetting the silence counter because the
  end threshold was tied to the low noise floor instead of the active speech level.
- `"Open the first link for the GitHub."` matched `open_app`.
- `"Fire Fox is open. Can you close the firefox for me?"` called `open_app` (no close tool).
- Fast-path replies and `"I didn't hear anything."` were printed but not spoken in `/voice`.

### Part 1 — sub-second end of speech (DONE)

- Each recording now keeps the speaker's **active speech level** (`speech_rms`, 75th
  percentile of the speech-like frame RMS values, newest 250 frames).
- A frame is silence when `rms < max(end floor, 0.35 × speech_rms)` **or**
  `rms <= 0.40 × speech_rms` (a ≥60 % drop). In-speech classification uses the same rule,
  so room noise above the low start threshold (live RMS 200–450) is no longer "speech".
- Last-resort stop: 2.5 s after the last frame at ≥45 % of the robust speech level
  (90th percentile) with low variance. This stops room/fan noise without a 30 s
  `max_length` run.
- `VOICE_SILENCE_MS` default **900** (range still configurable), `LISTEN_EARLY_CHECK_MS`
  default **600**, `MIN_SPEECH_BEFORE_SILENCE_STOP_MS` **400** (the old forced 1.5 s wait
  is gone, so "Ultron" / "mute" end immediately).
- Simulated user room (floor 100, speech RMS 1800 for 2 s, room RMS 450 afterwards):
  stops as `silence` at **900 ms after speech**, total capture 3.4 s, saved audio 2.5 s —
  **never `max_length`**. A 1000/400 case falls back to the 2.5 s backstop.

### Part 2 — wake word, hotwords, debug (DONE)

- Config env prefixes now include `LISTEN_`, `RECORDER_`, `WAKE_`, `SEARCH_` and
  `FIRST_RESULT_`. `LISTEN_DEBUG=1 .venv/bin/python main.py listen` now really enables
  the debug output (B3 merged only `GEMINI_/VOICE_/...` prefixes).
- `WHISPER_WAKE_HOTWORDS` (default `Ultron, Firefox, GitHub, Brave, Downloads`) is passed
  only by `listen` and `wake-train`; chat `/voice` keeps `WHISPER_HOTWORDS` blank.
- Built-in aliases now include the measured variants: `outron`, `ultra`, `hey ultron`,
  `i'm drawn`, `i'll draw`, `i'll drawn`, `i drawn`, `i've drawn`, `i'll try on`,
  `are you drawn`, `hey all drawn`, `hey all right`.
- Filler/connector stripping turns `"I'll draw an open Firefox."` into `"open firefox"`;
  nested wake spellings are stripped too (`"Ultron, I'll draw an open Firefox"`).
- Mishearings `open 5ox / 5x / file box / fire box / fire fox` become `open firefox`.
- Repeated wake words (`"Ultron, Ultron, Ultron"`) are treated as wake-only, not as a
  command and not as a repetition-loop rejection.
- Short (<1.8 s) wake word with `avg_logprob` in `[-1.35, -1.0)` is accepted as wake-only.
- `load_learned_aliases` drops ordinary single-word starters (`i`, `hey`, `what`, …) so
  the 18:01 `wake_aliases.json` cannot make every "I ..." sentence look like a wake word.
- **Measured on the 16 saved clips** (`workspace/wake_clips/`, real base.en, hotwords
  `Ultron, Firefox, GitHub, Brave, Downloads`):

| Group | Plain + aliases | Hotwords + aliases |
|---|---|---|
| 8 × single "Ultron" | 4/8 wake-only | **8/8 wake-only** |
| 4 × "Ultron open firefox" | 4/4 (one gave "firebox") | **4/4, all "open firefox"** |
| 4 × "Hey Ultron" | 0/4 commands (1 wake-only) | 0/4 (hotwords hurt this phrasing) |
| Total command/wake hits | 8/16 | **12/16** |

- The old word-list leak did not return: the 10 `workspace/test_clips/` stay 3–33 chars
  with hotwords; the longest saved wake transcript is < 120 chars.

### Part 3 — first link, app validation, close (DONE)

- `"Open the first link for the GitHub."` / `"open the first link on the GitHub"` /
  `"open first link of github"` / `"search github and open the first link"` route to
  `open_first_result(query="github")`; never `open_app`.
- `FIRST_RESULT_METHOD` default is now **google** (config + `.env.example` + `web.py`).
- `open_app` only resolves apps in `config/apps.yaml` (plus aliases). Unknown names return
  `unknown=True` and speak `"I don't know how to open <app>."`; `open_app_reply` now also
  checks the tool's own `ok`, so a failed call can never say "X is open.".
- New `close_app(app_name)` tool: whitelisted user GUI apps only (`firefox`, `brave`,
  `chrome`, `code`, `files/nautilus`, `vlc`, `spotify`, `calculator`, `text editor`,
  `terminal`, `settings`), `pgrep -x` → `kill -TERM` with argv lists, PID 1/self excluded,
  denied system processes (`gnome-shell`, `systemd`, `pipewire`, …).
- Replies from code: `"Closed Firefox."`, `"Firefox is not running."`,
  `"I don't know how to close <app>."`.
- `fast_path` matches direct and conversational close forms, including
  `"Fire Fox is open. Can you close the firefox for me?"`; `close_app` is registered in the
  CLI, GUI, hands-free registry and therefore in the LLM tool list.

### Part 4 — speaking and the conversational brain (DONE)

- `/voice` fast-path replies now print a speaking failure if Piper cannot play, and the
  `-> Speaking (Piper (...))...` line is emitted **before** playback, for every reply.
- The "I didn't hear anything." path is spoken on every consecutive silence turn
  (B3's `show_and_speak_outcome` now also reports a speak failure).
- Local prompt rules now say: answer questions directly with no tool, and only call
  `open_app`/`close_app` for an explicit open/launch/close/quit request.
- `AgentLoop` refuses an `open_app`/`close_app` call when the request contains no such
  intent, sends the refusal back, then answers once with tools disabled (tested).

### Part 5 — tests (DONE)

`tests/test_b4_live_bugs.py` adds deterministic coverage for: the 1800/450 room-noise
endpoint, the detector's 600 ms early + 900 ms final events, the real 16-clip hotword
comparison, repeated/short "Ultron", `"I'll draw an open Firefox."`, `"Ultron open 5ox"`,
first-link priority, the fake-app reply, `close_app` (closed / not running / refused),
`/voice` fast-path and consecutive no-hear speech, and `LISTEN_DEBUG` (env + one line per
utterance). Existing tests updated for the 900 ms default and the new close/first-link
behaviour.

### Verification (DONE)

- `.venv/bin/python -m pytest -q` → **328 passed, 0 skipped**.
- `.venv/bin/python -m compileall -q assistant main.py` → OK.
- `git diff --check` → no whitespace errors.
- Tests still redirect `logs/` and `workspace/` to a temp folder (conftest) and the
  real-log guard passes.
- `ss -ltnp | grep 9931` → **no llama-server running**.
- Nothing pushed; remote and git config untouched.

### Head state (B4)

- Branch `phase5-b4-work` (from `phase5-b3-work` @ `72a00f0`), local only, **nothing
  pushed**; remote and git config untouched.
- Commit `dc6cbdf` contains every code and test change plus the first docs update;
  `8e67094` adds this head-state section.
- Latest pytest: **328 passed, 0 skipped**.
- `ss -ltnp | grep 9931` → **no llama-server**.
- Working tree clean after the docs commit; `.env`, `models/`, `workspace/`, `logs/`,
  `.backups/` and `.venv/` were not staged or committed.

### Files changed

- Part 1: `assistant/speech/recorder.py`, `assistant/interfaces/listen.py`,
  `assistant/config.py`, `.env.example`.
- Part 2: `assistant/speech/wake.py`, `assistant/speech/ears.py` (no default change),
  `assistant/interfaces/listen.py`, `assistant/interfaces/wake_train.py`,
  `assistant/config.py`.
- Part 3: `assistant/brain/fast_path.py`, `assistant/brain/tool_replies.py`,
  `assistant/tools/apps.py`, `assistant/tools/web.py`, `config/apps.yaml`,
  `assistant/interfaces/cli.py`, `assistant/interfaces/gui.py`,
  `assistant/interfaces/listen.py`.
- Part 4: `assistant/interfaces/cli.py`, `assistant/interfaces/voice.py`,
  `assistant/brain/prompts.py`, `assistant/brain/agent_loop.py`.
- Tests: `tests/test_b4_live_bugs.py` (new), `tests/test_agent_rounds.py`,
  `tests/test_fast_path.py`, `tests/test_web.py`, `tests/test_wake_train.py`,
  `tests/test_listen.py`, `tests/test_listen_timing.py`,
  `tests/test_listening_reliability.py`, `tests/test_config_defaults.py`,
  `tests/test_reply_style.py`.
- Docs: `docs/PROGRESS.md`, `.env.example`.

### NEXT STEP — live tests (the coding tool cannot use the mic/speakers)

Run from `~/Downloads/assistant-project`, one command at a time:

1. `.venv/bin/python main.py listen`
   - Say **"Ultron"** → `· wake word heard`, `· waiting for your command (6 s)`,
     Ultron says "Yes?" within roughly a second of you stopping.
   - Say **"open firefox"** → Firefox opens, reply is spoken.
   - Say **"I'll draw an open Firefox"** or **"Ultron open 5ox"** → Firefox opens
     (mishearing repair).
   - Say **"open the first link for the GitHub"** → the first Google result opens.
   - Say **"Fire Fox is open. Can you close the firefox for me?"** → Firefox closes and
     Ultron says "Closed Firefox." (or "Firefox is not running.").
   - `scripts` should never run 30 s; if a command seems stuck, report the `Utterance:`
     line from `logs/assistant.log`.
2. `LISTEN_DEBUG=1 .venv/bin/python main.py listen` → every utterance prints
   `· debug: heard=... verdict=... wake_match=... score=... cmd=... aliases=...`.
3. `.venv/bin/python main.py chat` → `/voice`:
   - "open firefox" → spoken fast-path reply.
   - press Enter / stay silent twice → "I didn't hear anything." is shown **and spoken**
     both times.
   - "how are you?" → one short natural answer, no app opens.
4. Check `logs/assistant.log` for one `Latency:` line per accepted command
   (`transcribe`, `first_tool`, `reply_ready`, `speech_start`).

---

# Ultron progress — Phase 5 Part B3

Last updated: 2026-11-01 (B3 started: hands-free listen failed live)

## Phase 5 B3 (Nov 1) — CURRENT

Work branch: `phase5-b3-work` (from `phase5-b2-work` @ `71c4985`, local only, nothing
pushed). This section supersedes every section below it.

| Part | What | Status |
|------|------|--------|
| 1 | Audit every setting; permissions diff; "close" capability | DONE |
| 2 | Make `listen` visible; fix text leaks of ignored speech | DONE |
| 3 | Offline replay: why no command after "Yes?" | DONE |
| 4 | Adaptive warm-up; SNR thresholds from real clips | DONE |
| 5 | `main.py wake-train` + learned aliases, with safety rule | DONE (not run on his voice) |
| 6 | Speak every /voice outcome ("I didn't hear anything.") | DONE |
| 7 | First-result method without scraping | DONE |
| 8 | Docs, full pytest, commits | DONE |

### Live facts reported by the user (UNVERIFIED by me)

- Chat `/voice` + 10 s silence showed "I didn't hear anything." but nothing was spoken.
- `voice-test` accepted a clip with logged `snr 8.6 dB`, below `VOICE_MIN_SNR_DB=10`.
- `listen` showed only the start line; five transcripts in two minutes were his attempts
  at "Ultron": `outron.`, `I'm drawn.`, `I draw`, `I'll do it.`, `I'll turn.`; only
  `outron.` matched. No transcript after "Yes?".
- Speech text that did not pass the wake word appeared in `logs/assistant.log`, although
  the B2 report said it was not logged.
- `.env` currently contains only these matching keys (read with the approved grep):
  `WHISPER_MODEL`, `WHISPER_MODEL_DIR`, `WHISPER_HOTWORDS=`, `VOICE_REPLY_MAX_CHARS`,
  `VOICE_FALLBACK_*`, `MIC_SOURCE_VOLUME=0.21`. **`LISTEN_LOG_IGNORED` is not set**, so
  the "ignored" line could not appear (default 0).
- Volumes right now: sink 1.00, source 0.21.

### Part 1 result — settings audit (2026-11-01)

Read from `assistant/config.py`, `.env.example`, the approved `.env` grep, and a
`load_config()` call that printed only these names. `VOICE_FALLBACK_*` map to the
`tts_fallback_*` attributes, not `voice_fallback_*`.

| Setting | config default | .env.example | .env | effective | Verdict |
|---|---|---|---|---|---|
| VOICE_MODE | auto | auto | absent | auto | OK |
| VOICE_START_TIMEOUT_S | **10.0** (was 8.0) | 10 | absent | 10.0 | was WRONG, fixed |
| VOICE_SILENCE_MS | **2000** (was 1000) | 2000 | absent | 2000 | was WRONG, fixed |
| VOICE_MAX_SECONDS | 30.0 | 30 | absent | 30.0 | OK |
| VOICE_FIXED_SECONDS | 5.0 | 5 | absent | 5.0 | OK |
| VOICE_MIN_SNR_DB | 10.0 | 10 | absent | 10.0 | WRONG for his voice (8.6 dB clip); Part 4 |
| VOICE_VOLUME | 1.0 | 1.0 | absent | 1.0 | OK |
| VOICE_REPLY_MAX_CHARS | 160 | 160 | 160 | 160 | OK |
| VOICE_DEBUG | False | false | absent | False | OK |
| VOICE_HIGHPASS_HZ | 100.0 | 100 | absent | 100.0 | OK |
| VOICE_DENOISE_STRENGTH | 0.0 | 0 | absent | 0.0 | OK (A/B said off) |
| VOICE_FALLBACK_VOICE_TYPE / PITCH / RATE | tts_fallback_* male1/15/10 | male1/15/10 | male1/15/10 | same | OK |
| RECORDER_WARMUP_MS | 300 | 300 | absent | 300 | WRONG: pop lasts ~700–800 ms; Part 4 |
| MIC_SOURCE_VOLUME | "" | blank | 0.21 | 0.21 | OK, applied in `listen` too (silently) |
| MIC_SOURCE_NAME | "" | blank | absent | "" | OK (default source) |
| WAKE_WORD | ultron | ultron | absent | ultron | OK, but base.en is weak on it; Part 5 |
| WAKE_ALIASES | "" | blank | absent | "" | WRONG for his voice; Part 5 |
| LISTEN_EARLY_CHECK_MS | 800 | 800 | absent | 800 | OK |
| LISTEN_FOLLOWUP_S | 6.0 | 6 | absent | 6.0 | OK |
| LISTEN_AFTER_SPEECH_MS | 600 | 600 | absent | 600 | OK; first-word risk in Part 3 |
| LISTEN_LOG_IGNORED | False | 0 | **absent** | False | explains the missing "ignored" line |
| WHISPER_MODEL | base.en | base.en | base.en | base.en | OK |
| WHISPER_MODEL_DIR | models/faster-whisper-base.en | same | same | same | OK |
| WHISPER_HOTWORDS | "" | blank | blank | "" | OK (off) |
| WHISPER_MIN_AVG_LOGPROB | -1.0 | -1.0 | absent | -1.0 | OK |
| WHISPER_MAX_NO_SPEECH_PROB | 0.60 | 0.60 | absent | 0.60 | OK |
| WHISPER_MAX_COMPRESSION_RATIO | 2.4 | 2.4 | absent | 2.4 | OK |
| SEARCH_ENGINE | read by `web.py` | duckduckgo | absent | duckduckgo | OK (no config field) |

**What `listen` sends to Whisper** (`assistant/speech/ears.py transcribe_samples`):
`language="en"`, `beam_size=1`, `vad_filter=True`, `condition_on_previous_text=False`,
`without_timestamps=True`, no `initial_prompt`/`prompt`, `hotwords` only when
`WHISPER_HOTWORDS` is non-empty (it is blank), and a contiguous float32 array. **WRONG:**
`listen` did **not** run the `/voice` normaliser; it sent raw `int16/32768`. **Fixed in
B3:** `pcm_to_float32()` applies `normalise_mic_level(..., has_speech=True)` before
Whisper (and `wake-train` uses it too), so a quiet voice gets the same 3× repair as `/voice`.
Pinned by `test_listen_uses_the_same_level_repair_as_voice`.

**Thresholds** (code): start = `max(280, min(floor×2.5, 500), floor×10^(SNR/20))`, end =
`max(140, floor×1.8, min(start×0.6, speaker×0.18))`. With his logged floor 234 and
`VOICE_MIN_SNR_DB=10`, start = **740**, while his logged speech level was **629** (8.6 dB).
So the auto/listen gate can reject that quiet voice even though `voice-test` (fixed mode,
which uses the 10th-percentile floor) accepted it. That is why the logged 8.6 dB looked
inconsistent.

**Fix applied now:** the two dataclass defaults above now match the loader; tested in
`tests/test_config_defaults.py`.

**Volumes (read-only):** sink `Volume: 1.00`; source `Volume: 0.21`.

### Part 1b result — permissions diff

`git diff phase5-b1-work phase5-b2-work` shows only:
1. `GateDecision.needs_confirmation: bool = False` (new flag).
2. `PermissionGate.check(..., allow_prompt: bool = True)`. When a call would ask and
   `allow_prompt=False`, it returns denied + `needs_confirmation=True` **instead of reading
   the keyboard**.
3. `ToolRegistry(..., allow_prompts: bool = True)` (default True), a
   `needs_confirmation_seen` flag, and an `on_execute` timing hook. It passes
   `allow_prompt=self.allow_prompts` to the gate and records the refusal.

Nothing is auto-approved and no tier is weakened. Chat uses the defaults, so prompts are
unchanged; `listen` passes `allow_prompts=False`. Proven by
`tests/test_permissions_b3.py`: chat prompts and runs only after "yes", "no" runs nothing,
hands-free never calls the prompt callback and never runs the tool.

### Part 1c result — "close"

Registered tools: `list_files`, `read_file`, `open_app`, `volume_up`, `volume_down`,
`set_volume`, `mute_audio`, `web_search`, `open_first_result`. **None closes or quits an
app.** A grep for `close|kill|pkill|terminate|quit|wmctrl|xdotool` in `assistant/tools/`
found only the sandbox timeout helpers. B1 had no `fast_path.py` and no close path; the only
"quit/kill" words in B1's CLI/agent loop are `/quit` and the killswitch. So "close firefox"
never actually closed anything — the old model probably claimed it. B2's `fast_path.py`
recognises it and answers "I can't close apps yet." without acting. **Nothing built.**

### Part 2 result — visible `listen`, no ignored-speech text

- `run_listen()` now prints the same ready lines as chat: `  Mic input: …` (when
  `MIC_SOURCE_VOLUME` is set), `  Ears: ready — …`, `  Mouth: ready — …`.
- The start line is printed by the listener only after the warm-up, not before it.
- One INFO `Utterance:` line per processed utterance, **no words**: start/end clock time,
  duration, peak, rms, noise floor, SNR, end reason, `sent_to_whisper=yes`, result
  (`accepted`, `follow_up`, `wake_only`, `no_wake_word`, `rejected_by_guard`, `too_short`,
  `stop_command`).
- INFO lines for the discard window (open/closed) and follow-up window (open/closed).
- Screen lines only: `· heard speech, no wake word`, `· wake word heard`,
  `· waiting for your command (6 s)`, `· window closed` (plus the ready/start lines).
- `LISTEN_DEBUG=1` prints `· debug: heard "…"`, the wake score, the matched target and the
  aliases tried — **screen only**; never a log line or file.
- Privacy fix: `WhisperEars.transcribe_samples(..., log_text=False)` logs
  `text_chars=N` instead of `text='…'`; `listen` uses it. Non-wake speech is not saved
  (`save_debug` is only called after a wake match) and never reaches a tool, so it cannot
  reach `audit.jsonl`.
- Why `LISTEN_LOG_IGNORED=1` produced no line: the key is **not in `.env`**, so the
  effective value was 0. When it is set, only the first word is logged; the full text the
  user saw came from the old `Ears confidence … text='…'` line, which is now suppressed in
  listen.
- Tests: `test_ignored_speech_never_puts_its_words_in_the_log_or_on_screen`,
  `test_listen_log_ignored_prints_only_the_first_word`, `test_debug_text_is_screen_only`,
  and `test_private_transcription_keeps_the_words_out_of_the_log`.

### Part 3 result — why no command after "Yes?"

**Diagnosis on the OLD B2 code** (timed replay: a scripted mic, a fake Piper that blocks
1.0 s, a real saved command clip; a transcript is scripted by call order):

| Case | Old code result |
|---|---|
| command starts 200 ms after Piper | **FAILED** — `commands=[]`, ignored |
| 500 ms | **FAILED** — ignored |
| 1500 ms | **FAILED** — ignored |
| 5000 ms | **FAILED** — ignored |
| 6500 ms | passed (ignored, as intended) |

Instrumented run at 1500 ms:
`PROCESS at audio_ms=11060 followup_until=9440 active=False last_start=4680`.
The command **started at 4680 ms, inside the 6 s window**, but the window-closing check ran
on audio time and cleared `_followup_active` at 9440 ms while the user was still speaking.
At transcript time it was false, so the command was rejected as "no wake word". This is a
direct cause of the live "no transcript after Yes?".

The second cause: `LISTEN_AFTER_SPEECH_MS=600` discarded the first 600 ms after Piper, so
a command starting at 200/500 ms lost its beginning.

**Fixes:**
- The follow-up window closes only when the detector is **idle**, never mid-sentence
  (`UtteranceDetector.in_speech`).
- `LISTEN_AFTER_SPEECH_MS` default is now **300**.
- The last 400 ms of the discard window are kept and checked with `feed_warm()`: start
  detection is possible, but the noise floor/calibration are never touched (a burst cannot
  raise the thresholds). This keeps the first word.
- `ContinuousReader` (new): a reader thread pulls the mic into a bounded 40 s ring with
  timestamps while Whisper/LLM/Piper are busy; `clear()` drops only the busy-time backlog,
  so the next utterance is fresh and the pipe never fills.

**Tests:** `tests/test_listen_timing.py` (8: all four delays with the full clip intact,
6500 ms ignored, burst during Piper, two commands, "Ultron" twice) and
`tests/test_listen_reader.py` (2: buffering/clear while busy, and a live-paced source where
the command after Piper still runs).

### Part 4 result — adaptive warm-up and SNR from the real clips

**Adaptive warm-up** (`assistant/speech/recorder.py AdaptiveWarmup`): discard until two
consecutive 50 ms blocks are calm (≤ 1200 RMS, not rising), at least
`RECORDER_WARMUP_MS=300`, at most `RECORDER_WARMUP_MAX_MS=1500`. The pop never feeds the
noise floor, start detection or saved audio. It is used by `/voice`, `voice-test` and
`listen`; "Listening…"/"Recording…" prints only after it ends.

Measured on the saved takes:

| Take | warm-up end | room floor |
|---|---|---|
| 134518 (pop) | 780 ms | 320 |
| 135201 (fan pop) | 780 ms | 285 |
| 135223 (pop) | 840 ms | 335 |
| 134437 | 300 ms | 279 |
| 134532 | 360 ms | 299 |
| 134543 | 300 ms | 329 |
| 134606 | 300 ms | 302 |
| 134642 | 300 ms | 326 |
| 134700 | 300 ms | 313 |
| 134409 (loud throughout, transcript "Unused.", already rejected) | 1500 ms | **4510** |

So 9 of 10 takes end with a floor below 1000; the loud take is honestly reported, not
forced. `tests/test_warmup_snr.py` pins this.

**The 8.6 dB question:** the logged 8.6 dB is `speech_level (75th percentile of ALL
frames, including silence) / noise_floor`. The detector uses a **per-frame** test against
the floor, and on the same take speech frames are far above it. Measured after warmth:

| Clip | floor | speech (p75 of frames passing 10 dB) | SNR | detected |
|---|---|---|---|---|
| 134437 unmute | 245 | 4592 | 25.5 dB | yes |
| 134518 "Stay silent…" | 251 | 7016 | 28.9 dB | yes |
| 134532 Yes | 246 | 3731 | 23.6 dB | yes |
| 134543 "Can you unmute…" | 245 | 3054 | 21.9 dB | yes |
| 134606 "list files…" | 248 | 4314 | 24.8 dB | yes |
| **134642 "What is your name?"** | **255** | **3167** | **21.9 dB** | **yes** |
| 134700 "Turn the volume…" | 246 | 3948 | 24.1 dB | yes |
| 135223 "mute unmute" | 246 | 8138 | 30.4 dB | yes |
| fan 30 s | 286 | — (steady, only short gusts) | longest 10 dB run **80 ms** | **no** |
| constant 4700 | 2628 | none | < 10 dB | **no** |
| lone click | 40 | burst-excluded | — | **no** |
| loud 134409 | 4510 | 30971 | 16.7 dB | no (its speech is not the user's command) |

`VOICE_MIN_SNR_DB` stays **10** (a 5 dB gate let a fan gust run 740 ms and trigger). The
quietest real clip has ~11.9 dB of margin above the 10 dB gate.

**Two detector bugs found while measuring:**
1. The noise floor adapted on every frame under the start threshold; during speech it
   ratcheted from ~685 to ~2900, raising the gate above the user's own voice and preventing
   any start. It now adapts only when `level ≤ floor × 1.5`, slowly (0.95/0.05).
2. A 200 ms unbroken run is wrong for speech with syllable gaps. A leaky counter now adds
   20/250 per speech frame and subtracts 20/500 per other frame; start at 1.0 (250 ms of
   net speech). All 8 correct clips reach 1.0; fan 0.72, constant 0.72, click 0.08.

### Files changed so far (parts 3–4)

- `assistant/speech/recorder.py` (`AdaptiveWarmup`, activity counter, floor drift),
  `assistant/interfaces/listen.py` (`ContinuousReader`, window close, feed_warm),
  `assistant/interfaces/voice.py`, `assistant/config.py`,
  `.env.example`, `tests/test_listen_timing.py`, `tests/test_listen_reader.py`,
  `tests/test_warmup_snr.py`, `tests/test_listening_reliability.py`,
  `docs/PROGRESS.md`.

### Part 5 result — a wake word for his voice

- `assistant/interfaces/wake_train.py` (new) + `main.py wake-train`: one stream, waits for
  the adaptive warm-up, then asks for 8 × "Ultron", 4 × "Ultron open firefox", 4 ×
  "Hey Ultron". Each clip is saved in `workspace/wake_clips/` (because the user starts it)
  and transcribed four ways: base.en plain, base.en with `hotwords="Ultron"`, base.en with
  `vad_filter=False`, and small.en plain **only if** `models/faster-whisper-small.en`
  exists. It prints a table and the average seconds per variant.
- It writes `workspace/wake_aliases.json` with the first 1–3 words Whisper produced and
  their counts. It changes **no default**.
- `WAKE_ALIASES_FILE` (default `workspace/wake_aliases.json`) is loaded in addition to
  `WAKE_ALIASES`; matching tries the first one to three words, normalised.
- Safety rule: a built-in fuzzy match (`ultron`, `hey ultron`) works as before. A match to
  a **learned** alias is accepted only when the rest of the sentence is empty or matches a
  fast-path command; otherwise it is ignored silently. This stops "I'll turn" from running
  random talk.
- Tests: `tests/test_wake_train.py` (22) include the exact learned strings `outron.`,
  `I'm drawn.`, `I draw`, `I'll do it.`, `I'll turn.` each with a fast-path remainder
  (runs), alone (says "Yes?"), and with unknown text (ignored), plus ordinary sentences.
- **The aliases from his own voice are UNVERIFIED**: I cannot run `wake-train`. The built-in
  aliases are still only Piper-derived.

### Part 6 result — every /voice outcome is spoken

Root cause: `assistant/interfaces/cli.py` only spoke when **transcription** failed. A
10-second silence returns `(False, "I didn't hear anything.")` from `voice.record()`
earlier, and that branch only printed. B1 commit `ce9c67a` ("speak the no-hear message on a
voice turn") added `speak_sync` in the transcription branch, so it never ran for silence.
Fix: `show_and_speak_outcome()` prints and speaks exactly once, used for the record-failure
and transcription-failure paths; tool replies are spoken even when the task result is not
`ok`. Listening never restarts while Piper talks (the call is synchronous). Tests:
`tests/test_chat_commands.py` (nothing heard, guard rejection, tool reply → exact text
spoken once).

### Part 7 result — first result without scraping

`FIRST_RESULT_METHOD` = `ducky` (default) | `google` | `search`:
- ducky: `https://duckduckgo.com/?q=!ducky+<query>`
- google: `https://www.google.com/search?q=<query>&btnI=1`
- search: the normal search URL.
No HTTP request and no parser: the browser follows the redirect. Replies in code:
"Opening the first result for <query>." (ducky/google) and "Opening the search results for
<query>." (search). argv lists only, http/https only, query ≤ 200 chars. The old scraping
parser/fixtures are kept and tested, but the tool no longer calls them. **Whether the
browser lands on the first result is UNVERIFIED** (only the user can try it).

### Open problems (B3)

- Every B3 fix is offline-tested only; the live microphone and speakers are UNVERIFIED.
- `wake-train` has not been run on his voice; learned aliases do not exist yet.
- `ducky`/`google` first-result depend on the browser following the redirect; UNVERIFIED.
- The loud take `134409` has a real floor of ~4510 (reported, not forced below 1000).
- `.env` does not set `WAKE_ALIASES_FILE`, `LISTEN_DEBUG` or the new warm-up max; defaults
  apply (`workspace/wake_aliases.json`, 0, 1500 ms).

### Head state (B3)

- Branch `phase5-b3-work` (from `phase5-b2-work` @ `71c4985`), local only, nothing pushed.
- Commits: `763628d` checkpoint, `4ce734e` part 1, `0e9bb74` part 2, `a7819ed` parts 3–4,
  `91615b0` parts 5–7, `dee1123` listen normaliser, and the final docs commit after it.
- Latest pytest: **300 passed, 0 skipped**. No llama-server (`ss -ltnp | grep 9931` empty).
- Working tree clean; `.env` untouched and not committed; nothing pushed.

### Part 8 result

Full `.venv/bin/python -m pytest -q` → **300 passed, 0 skipped**. No llama-server on 9931.
Every file is committed locally on `phase5-b3-work`; nothing was pushed.

### NEXT STEP — live tests (the coding tool cannot use the mic/speakers)

Run these in order and report each result.

1. `.venv/bin/python main.py wake-train` → 16 cues (8 × "Ultron", 4 × "Ultron open
   firefox", 4 × "Hey Ultron"). Expected: one table with plain/hotwords/no-vad/small.en
   and the average seconds; `workspace/wake_aliases.json` created. **Review the table and
   choose an alias yourself; no default was changed.**
2. `LISTEN_DEBUG=1 .venv/bin/python main.py listen`. Expected ready lines (Mic/Ears/Mouth),
   then `Listening. Say 'Ultron, <command>'. Ctrl+C to stop.` after the pop. Say
   **"Ultron"** → screen shows `· wake word heard`, `· waiting for your command (6 s)`, and
   Ultron says "Yes?". Then say **"open firefox"** within 6 s → Firefox opens. Debug lines
   show what Whisper heard, the score and the aliases.
3. In the same session say **"Ultron open firefox and search for github"** → Firefox opens
   the search page and Ultron says "Searching for github in Firefox." (or, for "open the
   first result for github", "Opening the first result for github.").
4. `.venv/bin/python main.py chat` → `/voice`, **10 s silence, hands off the keyboard,
   speaker audible**. Expected: the screen shows `Voice: I didn't hear anything.` **and
   Piper speaks it once**.
5. Repeat step 2–4 with the fan **ON** and **OFF**. Expected: the fan alone never wakes it
   (no line, no command); a normal sentence still works. Report the `Utterance:` log lines
   (duration, floor, snr, result) if anything is missed.

---

# Ultron progress — Phase 5 Part B2

Last updated: 2026-11-01 (Phase 5 B2 finished; live tests pending)

## Phase 5 B2 (Nov 1) — HISTORY (superseded by B3 above)

Work branch: `phase5-b2-work` (local only, nothing pushed). This section is the current
state; every section below it is older history and is kept only as a record. Baseline
before any B2 edit: `.venv/bin/python -m pytest -q` → **174 passed, 3 skipped** (the 3
skips were the real-clip tests whose `/tmp/cmp2_*.wav` files are gone; Part 1 replaced
them with the saved live attempts).

| Part | What | Status |
|------|------|--------|
| 1 | Evidence + test hygiene (tmp logs, real-log guard, real clips) | DONE |
| 2 | Fix Bug A: recorder warm-up, SNR start, "nothing heard" | DONE (code + tests) |
| 2B | Noise robustness (fan analysis, SNR, denoise A/B, noise plan) | DONE (code + docs; live UNVERIFIED) |
| 3 | Hands-free `main.py listen` (wake word, early endpoint, follow-up) | DONE (offline tests; live UNVERIFIED) |
| 4 | Fast path for simple commands | DONE (code + tests) |
| 5 | Web tools (`web_search`, `open_first_result`) | DONE (fallback verified; real GET blocked by DuckDuckGo) |
| 6 | Typing detection report (`docs/TYPING_PLAN.md`) | DONE (detect only) |
| 7 | Docs, full pytest, commits | TODO |

### Part 1 result — evidence and test hygiene

- Added `tests/conftest.py`: before any test module imports `assistant.config`, it sets
  `ULTRON_LOGS_DIR` and `ULTRON_WORKSPACE_DIR` to a session temp folder. An autouse
  fixture snapshots the real `logs/assistant.log` and `logs/audit.jsonl` before/after
  every test and fails the test if either changed.
- `assistant/config.py`: `LOGS_DIR` and `WORKSPACE_DIR` can be redirected by those two
  env vars (normal runs use the project folders, so production behaviour is unchanged).
- `tests/test_log_hygiene.py`: proves the redirect is active and that a real tool call is
  logged in the temp file, never in the real log.
- `tests/test_listening_reliability.py`: real clips are looked up in
  `workspace/test_clips/` first and `/tmp` second. The three previously skipped tests now
  run from the saved live attempts (expected text read from each JSON trace, only
  `verdict == "ok"` clips). The two-clip test joins the real "download" and "unmute"
  attempts. All 17 tests in the file pass, 0 skipped.
- `workspace/test_clips/` did not exist; it now contains copies (no deletions) of the
  10 saved attempts. They are git-ignored.

### Part 2 result — Bug A

**Which level pattern made 29.4 s count as speech:** replaying
`attempt_20261101-135201-414426` (the 30 s fan take) through the unchanged recorder
reproduces `reason=max_length, duration=30.0 s, speech=3.9 s` exactly. The trace has
`noise_floor=23186`, `start=500`, `end=41734`.

**Why the end-of-speech rule never fired:** the first 200 ms calibration picked up the
stream-opening burst, so the noise floor became 23186. The start threshold is capped at
500, but the end threshold is `max(140, 1.8 × noise_floor, …)` = 41734, which is above the
int16 maximum. The hysteresis band therefore collapsed: every ordinary fan frame is
"silence", but any fan gust ≥500 is "speech" and resets the 2 s silence counter. In the
30 s file, 40 of 300 100 ms windows are ≥500 and they occur less than 2 s apart, so
`silence_run` never reaches 2000 ms and only the 30 s cap stops it. Saving the file from
the detected "speech start" is why the first 29.4 s looked like speech.

**Start-of-stream burst table** (RMS per 50 ms for the first 600 ms of every saved attempt):

| Attempt | 0–50 | 50–100 | 100–150 | 150–200 | 200–250 | 250–300 | Burst? |
|---|---|---|---|---|---|---|---|
| 134409 | 5991 | 6048 | 6618 | 5933 | 5875 | 5274 | no (loud room) |
| 134437 | 320 | 340 | 338 | 308 | 294 | 303 | no |
| 134518 | 30016 | 32768 | 32768 | 32768 | 32456 | 25361 | **yes** |
| 134532 | 335 | 289 | 307 | 401 | 458 | 348 | no |
| 134543 | 315 | 312 | 351 | 292 | 328 | 388 | no |
| 134606 | 477 | 418 | 321 | 319 | 281 | 316 | no |
| 134642 | 418 | 560 | 492 | 517 | 566 | 389 | no |
| 134700 | 351 | 349 | 334 | 302 | 381 | 385 | no |
| 135201 | 32750 | 32625 | 29351 | 22650 | 16937 | 12515 | **yes** |
| 135223 | 30428 | 32768 | 32768 | 32768 | 31274 | 22960 | **yes** |

A start-of-stream burst is **CONFIRMED in the saved audio** (3 of 10 takes start at or
near full scale and decay over ~300–400 ms). Its cause (pw-record opening the stream) is
**UNCONFIRMED**: the recorder log cannot see the device, only the samples.

**Fix (smallest change that works):**

- `assistant/speech/recorder.py`: the first `warmup_ms` (default **300**, config
  `RECORDER_WARMUP_MS`) of every stream is ignored entirely — no noise-floor calibration,
  no start detection, no saved audio.
- Start requires **both** the old capped level **and** `level ≥ noise_floor × 3.16`
  (10 dB, config `VOICE_MIN_SNR_DB`). Steady fan noise defines the floor, so it can never
  pass, however loud it is.
- A perfectly steady level for 8 s (CV < 0.06) is abandoned as noise (`constant_noise`).
- `assistant/interfaces/voice.py`: any capture with no real speech now returns
  **"I didn't hear anything."** (timeout, steady noise, max_length without a voice).
- `assistant/speech/ears.py`: a transcript with no real word is "nothing heard", not
  "say it again". `UNCLEAR_SPEECH` is kept only for real words with bad confidence.
- `has_speech_energy()` (fixed mode) now ignores clipped bursts and uses a 10th-percentile
  floor; `trim_leading_transient()` no longer eats speech in short windows.

**Verified:** 30 s of constant noise at rms 4700 with a loud burst in the first 20 ms →
no speech, nothing sent to Whisper. The 30 s fan take → timeout, nothing sent. All 8 live
attempts with a correct transcript are still detected. 17 listening tests pass.

### Part 2B result — noise robustness (the fan)

**Fan analysis** (`attempt_20261101-135201-414426.wav`, steady stretch 1.5–29.9 s, numpy
FFT):

| Measure | Value |
|---|---|
| RMS | ≈ 393 (int16 scale), ≈ −38 dBFS |
| Sample peak | 4910 |
| Energy below 100 Hz | **40.1 %** |
| 100–300 Hz | 14.1 % |
| 300–1000 Hz | 23.9 % |
| above 1000 Hz | 21.9 % |
| RMS per 100 ms (min / median / max) | 265 / 352 / 1025 |

Meaning: mostly **low-frequency rumble** (fan/air vibration) plus a steady broadband
hiss. It is steady (median 352, gusts up to 1025), not speech-like modulation. Top FFT
bins are 1–3 Hz (very slow rumble), so a 100 Hz high-pass removes a large share.

**SNR rule (implemented in Part 2):** a frame is speech only if its level is above the
capped threshold **and** at least `VOICE_MIN_SNR_DB` (default **10**) above the running
noise floor. Since steady noise defines that floor, a louder fan raises its own floor and
can never pass. Measured: the 30 s fan take now ends as `timeout` with no speech; 30 s of
constant synthetic noise at rms 4700 with a burst in the first 20 ms also gives nothing.

**Denoise A/B** (production `WhisperEars`, 8 real clips with a correct transcript; fan
mixed at 0/5/10 dB SNR; case/punctuation ignored; exact matches):

| Version | 0 dB | 5 dB | 10 dB |
|---|---|---|---|
| clean + fan | 4/8 | 6/8 | 8/8 |
| clean + fan + denoise | 4/8 | 6/8 | 8/8 |

- Clean clips: **8/8** transcribed exactly.
- Denoising the clean clips: **7/8** (`Can you unmute the voice please?` became
  `Can you unmute your voice please?`).
- Denoise never improved a noisy match and made two sentences shorter
  (`Stay silent with me.` → `Stay silent.`).
- Whisper time stayed ≈ 0.37–0.48 s per clip with and without denoise.

**Decision: `VOICE_DENOISE_STRENGTH` ships OFF (default 0).** The rule requires the clean
clips to stay exact and noisy matches to improve or stay equal; the clean clips got worse,
so OFF is the honest choice. `assistant/speech/denoise.py` (numpy only, high-pass +
stationary spectral gate from the quietest frames, max strength 0.8) is kept and tested so
the user can try `VOICE_DENOISE_STRENGTH=0.6`. This is UNVERIFIED on his live voice.

**Mic source:** `pw-record --help` supports `--target`, so `MIC_SOURCE_NAME` was added
(blank = default source). The "Mic level:" log line now includes the noise floor and SNR.

**Noise plan:** `docs/NOISE_PLAN.md`. Installed now: RNNoise package **not installed**;
`/usr/lib/ladspa` and `/usr/lib64/ladspa` **do not exist**. One source exists:
`Built-in Audio Analog Stereo`, `node.name=alsa_input.pci-0000_00_1f.3.analog-stereo`.
The exact `sudo dnf install ladspa-noise-suppression-for-voice` + filter-chain steps are
written there and marked UNVERIFIED (the coding tool ran no sudo/dnf).

### Part 3 result — hands-free `main.py listen`

- `assistant/speech/wake.py` (new): fuzzy wake matching (difflib ratio ≥ 0.75) on the
  first one or two words, plus `sentence_is_complete()` for the dangling-word rule.
- `assistant/interfaces/listen.py` (new): `UtteranceDetector` (20 ms frames, 300 ms
  warm-up, 400 ms ring buffer, 250 ms to start, the same hysteresis/SNR thresholds) and
  `HandsFreeListener` (one long-lived source for the whole session, early check, retry,
  follow-up window, silence on ignored speech, latency line, debug only after the wake
  word). `run_listen()` builds the real session (mic, ears, mouth, tools, agent loop).
- `assistant/speech/ears.py`: `transcribe_samples()` accepts a float32 array, so ignored
  speech is never written to disk.
- `assistant/safety/permissions.py` + `tools/registry.py`: `allow_prompt=False`; a tool
  that would ask returns `needs_confirmation=True` and is **not run**. `listen` speaks
  "That needs confirmation. Please type it in the chat."
- `main.py`: new `listen` command; it starts/stops llama-server exactly like `chat`.
- `assistant/interfaces/cli.py`: registers the web tools and uses the fast path on voice
  turns.
- Settings (defaults): `WAKE_WORD=ultron`, `WAKE_ALIASES=` (blank),
  `LISTEN_FOLLOWUP_S=6`, `LISTEN_EARLY_CHECK_MS=800`, `LISTEN_AFTER_SPEECH_MS=600`,
  `LISTEN_LOG_IGNORED=0`, `RECORDER_WARMUP_MS=300`.
- Wake aliases from Piper evidence (WAV files only, never played): `Ultron, mute.` and
  `Ultron, list my downloads.` came out clean; `Ultron open firefox` became
  `Ultra-open Firefox`; `Ultron search github` became `Ultra on Search Kit Home`;
  `Hey Ultron, open firefox` stayed `Hey Ultron, open Firefox.`. Built-in aliases are
  therefore **`ultra`** and **`hey ultron`**. Piper's voice is not the user's voice, so
  they are UNVERIFIED until his live test.
- Tests: `tests/test_listen.py` (12 required cases + permission refusal + window expiry +
  debug-only-after-wake) and `tests/test_wake.py` (19 wake/dangling cases).

### Part 4 result — fast path

- `assistant/brain/fast_path.py` (new): whole-sentence match for `open X`, `close X`,
  `mute`, `unmute`, `turn/set volume to N`, `list files in <folder>`, plus the Part 5 web
  sentences. Everything runs through `ToolRegistry.execute`, so permission modes are
  unchanged. Anything unclear returns `None` and goes to the LLM. One INFO line says
  `Fast path: used …` or `Fast path: LLM used …`.
- `close X`: this project has **no close tool**, so it is recognised and answered
  honestly with "I can't close apps yet." (no tool, no LLM, no invented action).
- Tests: `tests/test_fast_path.py` (10 matched sentences, close, one LLM-only sentence,
  multi-step rejection, exact reply strings).

### Part 5 result — web tools

- `assistant/tools/web.py` (new): `web_search(query, browser=None)` and
  `open_first_result(query)`. URL built in code (`SEARCH_ENGINE`, default duckduckgo),
  queries capped at 200 chars with control characters replaced, argv lists only, only
  http/https, no typing.
- Replies in code: "Searching for <query> in <browser>." and
  "Opened the first result for <query>: <domain>."
- **Real GET result (2026-11-01): HTTP 202 from `https://html.duckduckgo.com/html/?q=github`
  with 14 246 bytes, but the page was DuckDuckGo's bot challenge ("Unfortunately, bots use
  DuckDuckGo too … select all squares containing a duck"), so the parser found **no**
  result and `fetch_first_result("github")` returned `""`.** The implemented fallback is
  therefore what runs on this machine: it opens the normal search page and says
  "I couldn't pick the first result, so I opened the search results." The parser itself is
  proven on a synthetic result page. The real first-result method is **UNVERIFIED** (it
  was blocked).
- Fixtures: `tests/fixtures/ddg_results.html` (synthetic markup) and
  `tests/fixtures/ddg_challenge.html` (trimmed real challenge page, 268 bytes, no personal
  data).
- Registered in CLI and `listen` exactly like the other tools.
- Tests: `tests/test_web.py` (query cleaning, encoding, parser, challenge, redirect
  unwrap, refusal of file/javascript URLs, argv list, browser URL, success and fallback
  replies).

### Part 6 result — typing detection (nothing built)

`docs/TYPING_PLAN.md`. Session is **Wayland**. Installed: `ydotool 1.0.4-8.fc44` with
`/usr/bin/ydotoold` and a **system** unit `/usr/lib/systemd/system/ydotool.service`;
`wl-copy`/`wl-paste` exist; `dotool` is **not** installed. `systemctl is-active ydotool` →
`inactive`, `is-enabled` → `disabled`. `/dev/uinput` exists (`root:root`, mode 660) **with
an ACL `user:nitinyadav:rw-`**, so this user has access without joining a group. The exact
sudo commands for the user and the risks are in the plan.

### Files changed (parts 3–6)

- New: `assistant/interfaces/listen.py`, `assistant/speech/wake.py`,
  `assistant/brain/fast_path.py`, `assistant/tools/web.py`,
  `tests/test_listen.py`, `tests/test_wake.py`, `tests/test_fast_path.py`,
  `tests/test_web.py`, `tests/fixtures/ddg_results.html`,
  `tests/fixtures/ddg_challenge.html`, `docs/TYPING_PLAN.md`.
- Changed: `assistant/speech/ears.py`, `assistant/interfaces/cli.py`,
  `assistant/brain/tool_replies.py`, `assistant/tools/registry.py`,
  `assistant/safety/permissions.py`, `assistant/config.py`, `main.py`, `.env.example`,
  `docs/PROGRESS.md`.

### Files changed so far

- `assistant/config.py`, `assistant/speech/recorder.py`, `assistant/speech/ears.py`,
  `assistant/interfaces/voice.py`
- `tests/conftest.py` (new), `tests/test_log_hygiene.py` (new),
  `tests/test_listening_reliability.py`, `tests/test_voice_auto.py`
- `docs/PROGRESS.md`

### Open problems

- The two attempts named in the task are pruned; `135201` is the preserved equivalent.
- The fan/noise rules are proven offline only; the live microphone is still UNVERIFIED.
- The wake aliases come from Piper's synthesized voice, not the user's; UNVERIFIED live.
- DuckDuckGo's HTML endpoint returned its bot challenge (HTTP 202), so the
  first-result method could not be proven; the fallback path is what runs.
- `close <app>` is recognised but there is no close tool, so it answers honestly instead
  of acting.
- One-off analysis scripts (A/B, Piper wake evidence) wrote `Ears`/`Piper` lines into the
  real `logs/assistant.log` around 14:55. Tests themselves are now isolated and cannot do
  this.

### Head state

- Branch `phase5-b2-work`, local commits only, **nothing pushed**, remote and git config
  untouched:
  - `994f85a` start checkpoint
  - `9c9f90a` parts 1–2 (test log isolation, Bug A)
  - `9eaf686` part 2B (fan SNR gate, optional denoise, noise plan)
  - `c07e24b` parts 3–6 (hands-free listen, fast path, web tools, typing plan)
  - `3beb29d` part 7 (head state + live tests)
  - `ff2013b` polish (minimal `listen` output, true speech-start delay)
- Latest pytest: **248 passed, 0 skipped**. `ss -ltnp | grep 9931` → **no llama-server**.
- Working tree clean; `.env` untouched and not committed.

### How to resume this session

1. Read this B2 section (it is the current state).
2. `git switch phase5-b2-work`; `git status` and `git log --oneline -5`.
3. `.venv/bin/python -m pytest -q` → expect **248 passed, 0 skipped**.
4. Run the live tests in **NEXT STEP — live tests** below.
5. Do not start Phase 5 Part C without the user's approval.

### NEXT STEP — live tests (the coding tool cannot use the mic)

Run these and report each result. Do not start Phase 5 Part C without approval.

1. `.venv/bin/python main.py chat` → `/voice`, then **say nothing for 10 seconds with hands
   off the keyboard**. Expected: **"I didn't hear anything."**, no transcript, no tool
   call, no long 30 s recording. (This is the Bug A test.)
2. With the fan **ON**: `.venv/bin/python voice-test`, say "open firefox and list my
   downloads" normally. Then run `/voice` in chat and stay silent for 10 s with the fan on.
   Expected: the sentence is transcribed; the silence says "I didn't hear anything."
3. `.venv/bin/python main.py listen`. Expected start line exactly:
   `Listening. Say 'Ultron, <command>'. Ctrl+C to stop.`
   Say **"Ultron open firefox and search for github"**. Expected: Firefox opens the
   DuckDuckGo results page and Ultron says "Searching for github in Firefox." (or, if the
   first-result path is used, "Opened the first result for github: …").
   Then say **"Ultron stop listening"**. Expected: it stops, the prompt returns, no
   `pw-record` process and no llama-server are left.
4. In `listen`, say only **"Ultron"**. Expected "Yes?"; then say **"open firefox"** without
   the wake word and it must run. Wait 7 s and repeat "open firefox" without the wake word:
   it must be ignored (no print, no tool).
5. In `listen`, talk to someone else for a few seconds without "Ultron". Expected: silence,
   no printed line, nothing saved in `workspace/voice_debug/`.
6. Check `logs/assistant.log` for one `Latency:` line per accepted command and for
   `Fast path: used` on "open firefox"/"mute"/"volume".
7. If the wake word is missed, set `WAKE_ALIASES` in `.env` to the word Whisper heard
   (for example `WAKE_ALIASES=ultra,computer`) and report the first word from the log.

### Evidence checked so far

- `docs/phase5-b1-notes.md` named in the task does **not exist** (only
  `docs/ULTRON_LOCAL_HANDOFF.md` and this file).
- `workspace/test_clips/` did **not exist** at session start. It now holds copies (no
  deletions) of all 10 saved `workspace/voice_debug/attempt_*` WAV+JSON pairs.
- The two attempts named in the task (`134335-107538`, and the old 13:45 one) are no
  longer in `voice_debug` — only the newest 10 attempts are kept. The equivalent 30 s
  fan recording is `attempt_20261101-135201-414426` (reason `max_length`, duration
  30.0 s, `speech_ms` 3900, text `''`, verdict `no_real_word`, `noise_floor=23186`,
  `start=500`, `end=41734`).
- Start-of-stream burst: 3 of 10 saved attempts start at near full scale and decay over
  ~300-400 ms (`134518`, `135201`, `135223`). The other attempts start quiet.
- `/tmp/cmp2_*.wav` are gone.

### Files changed (summary)

Every file is listed in the part sections above and committed on `phase5-b2-work`
(`994f85a`, `9c9f90a`, `9eaf686`, `c07e24b`, `3beb29d`, `ff2013b`).

---

Last updated: 2026-11-01 (full session handoff — Phase 5 Part B1)

## HANDOFF — Phase 5 Part B1 (HISTORY, superseded by Phase 5 B2 above)

### Project in one paragraph

Ultron is a Fedora 44 voice-controlled laptop assistant (Python, venv `.venv`, folder
`~/Downloads/assistant-project`). It runs fully locally: llama.cpp LFM3-1.7B brain at
`http://127.0.0.1:9931/v1`, faster-whisper `base.en` ears on CPU, Piper mouth on CPU.
`BACKEND=local` is the default and needs no API key; the Gemini cloud rollback
(`BACKEND=gemini`) still exists. GTX 1650 (4 GB) is reserved for the LLM.

### Where the work stands

- Branch: **`phase5-b1-work`** (from `main` @ `9d95911`). **Nothing has been pushed.**
  The user pushes himself when he says so. Remote/git config were never touched.
- Phase 5 Part A is done. Phase 5 Part B1 is done, including the listening fix and the
  polish after live tests. **Phase 5 Part C (15-command benchmark) has NOT started** —
  do not start it without the user's approval.
- Latest commit on the branch: **`0816c42`** (this handoff; the detailed work commit is
  `6853561`). Full commit list is in "Head state (polish after live tests)" below.
- Latest pytest: **177 passed**. `ss -ltnp | grep 9931` shows **no llama-server running**.
- Working tree is clean. `.env` was not committed.
- Last live session (12:24–12:31) transcribed **8 of 8 voice turns correctly**; guard scores
  were far from the limits (avg −0.089…−0.343, no_speech ≤ 0.10, compression ≤ 1.233).
- Mic source volume is **0.21**; `.env` now contains `MIC_SOURCE_VOLUME=0.21` (appended
  without reading/printing; it is applied once at `chat` and `voice-test` startup).

### What works now

- **Listening** (`assistant/speech/recorder.py`, `assistant/interfaces/voice.py`):
  auto mode listens until 2 s of continuous silence, keeps 400 ms pre-roll, start timeout
  10 s, hysteresis/adaptive start and end thresholds, ignores saturated wake-up thumps,
  Enter finishes (and is consumed), Ctrl+C cancels only the listen. Fixed mode streams the
  same way. Every attempt saves a raw WAV + JSON trace in `workspace/voice_debug/`
  (newest 10) and writes one INFO line.
- **Transcription** (`assistant/speech/ears.py`): faster-whisper `base.en`, CPU int8,
  float32 numpy arrays only (never a path). Strict "say it again" guard: reject if any one
  of `avg_logprob < -1.0`, `no_speech_prob > 0.60`, `compression_ratio > 2.4`, or the text
  has no real word / is a repetition loop. Scores are logged for every transcript.
- **Normalisation** (`assistant/speech/recorder.py`): gain is capped by the true raw peak
  (`0.9 × 32767 / peak`), so output never clips; non-clipping input is never attenuated.
- **Mic input level** (`assistant/speech/mic_input.py`): optional `MIC_SOURCE_VOLUME`
  (0.05–0.6) applied once at chat/voice-test start via `wpctl`; failures only log.
- **Commands** (`assistant/interfaces/cli.py`): trim + collapse leading slashes
  (`//voice` → `/voice`); slash lines are always commands; unknown commands print
  "Unknown command. Available: /model, /thinking, /voice, /mode, /dry-run, /logs, /quit"
  and never reach the LLM; slash lines and bare `voice`/`quit`/`logs` are logged with repr.
- **Local replies** (`assistant/brain/agent_loop.py`, `tool_replies.py`, `reply_style.py`,
  `local_client.py`): code-built replies for list_files, mute_audio, set_volume,
  volume_up/down; mute/unmute is forced from the spoken word and is idempotent; ambiguous
  mute asks "Do you want to mute or unmute?"; timed mute gets an honest "no timer" answer
  and the following "yes" mutes directly; set_volume requires the number in the transcript;
  stop sequences + repeat penalty + a cleaner remove `<|...|>` fragments and repeated
  sentences.
- **Tools** (`assistant/tools/`): `list_files`, `read_file`, `open_app`, `volume_up`,
  `volume_down`, `set_volume`, `mute_audio`; local runs cap at 3 tool rounds.
- **Safety**: permission gate/modes, killswitch, audit log, redaction, sandbox helpers are
  unchanged. Tool calls are logged at INFO with redacted arguments.

### Key files

| Area | File |
|------|------|
| Entry point | `main.py` (`chat`, `voice-test`, `doctor`, model download) |
| CLI / voice loop | `assistant/interfaces/cli.py` |
| Mic capture / level | `assistant/speech/recorder.py`, `assistant/interfaces/voice.py` |
| Ears (Whisper) | `assistant/speech/ears.py` |
| Mouth (Piper) | `assistant/speech/mouth.py` |
| Mic startup level | `assistant/speech/mic_input.py` |
| Agent loop | `assistant/brain/agent_loop.py` |
| Local LLM client | `assistant/brain/local_client.py` |
| Prompts / reply shaping | `assistant/brain/prompts.py`, `reply_style.py`, `tool_replies.py` |
| Tools | `assistant/tools/files.py`, `apps.py`, `volume.py`, `registry.py` |
| Config | `assistant/config.py`, `.env` (not committed), `.env.example` |
| Voice diagnostics | `assistant/doctor.py` |
| Tests | `tests/` (see `pytest.ini`) |
| Debug captures | `workspace/voice_debug/` (git-ignored, newest 10) |
| Runtime logs | `logs/assistant.log`, `logs/audit.jsonl`, `logs/llama-server.log` |

### Configuration (non-secret)

- `BACKEND=local`; `LLAMA_BASE_URL=http://127.0.0.1:9931/v1`; `WHISPER_MODEL=base.en`.
- Voice: `VOICE_MODE=auto`, `VOICE_START_TIMEOUT_S=10`, `VOICE_SILENCE_MS=2000`,
  `VOICE_MAX_SECONDS=30`, `VOICE_FIXED_SECONDS=5`, `VOICE_DEBUG=false`.
- `MIC_SOURCE_VOLUME=0.21` (in `.env`; blank in `.env.example` means "do nothing").
- Guard: `WHISPER_MIN_AVG_LOGPROB=-1.0`, `WHISPER_MAX_NO_SPEECH_PROB=0.60`,
  `WHISPER_MAX_COMPRESSION_RATIO=2.4`. `WHISPER_HOTWORDS` stays blank/off.
- `VOICE_REPLY_MAX_CHARS=160`; Piper voice `en_US-lessac-medium`.
- Do not change llama-server launch flags, permission modes, Piper settings or the Gemini
  rollback. Do not install/downgrade packages.

### Known limitations / risks

- The polish changes have **not yet been verified with the live microphone** (the coding
  tool cannot use the mic). The next live test list is below.
- PyAV 19 breaks faster-whisper 1.2.1 when a file path is passed; production always passes
  float32 arrays, and a test enforces it. The user may later pin `av<19`.
- There is no mute timer. Timed requests are refused honestly; only "yes" to the offer
  mutes now.
- The stop sequences are LFM3's ChatML tokens (`<|im_start|>` etc.); update them if the
  model changes. `repeat_penalty` is 1.15.
- Mic source is set to 0.21 at every chat/voice-test start, which will override a manual
  change while the setting is in `.env`.
- `workspace/voice_debug/` and logs contain speech transcripts; keep private.
- The model can still mishear; the guard and intent checks reduce wrong actions but do not
  make the local model perfect.

### NEXT STEP — live tests (do these first)

The coding tool must not run `main.py chat` or voice tests. Ask the user to run and report:

1. `.venv/bin/python voice-test` — expect `[Mic] Mic source volume set to 0.21`, then say
   "open firefox and list my downloads" and press Enter; expect a complete transcript.
2. `.venv/bin/python main.py chat`:
   - **REAL silence test**: `/voice`, say nothing for 10 seconds → hear
     "I didn't hear anything.", no transcript, no tool call.
   - `/voice` "mute" → "Audio is now muted."; `/voice` "unmute" → "Audio is now unmuted."
   - `/voice` "mute unmute" → "Do you want to mute or unmute?" (no tool call).
   - `/voice` "stay silent for 10 seconds" → honest timer reply; say "yes" → mutes now.
   - Type `//voice` → starts listening (not sent to the LLM).
   - Type `/nonsense` → unknown-command line, no LLM answer.
   - `/voice` "what is your name?" → "I am Fedora GNOME voice assistant, Ultron."
   - `/voice` "turn volume to 30 percent" → "Volume is now set to 30 percent."
   - Check `logs/assistant.log`: every `Mic level` line has `after peak` ≤ ~29490.
3. If anything fails, keep the newest `workspace/voice_debug/attempt_*.json` and the log
   tail. If all pass, the next project step is Phase 5 Part C — only with the user's OK.

### How to resume next session

1. Read `AGENTS.md` (it points here) and this handoff section.
2. `git switch phase5-b1-work`; run `git status` and `git log --oneline -10`.
3. Run `.venv/bin/python -m pytest -q` (expect 177 passed).
4. Continue from **NEXT STEP — live tests** above.
5. Work rules: one part at a time, update this file before each part and after 2–3 edits,
   run `.venv/bin/python -m pytest -q` after each change, commit locally by file name only,
   never push, never commit `.env`/`models/`/`workspace/`/`logs/`/`.backups/`/`.venv/`,
   never run sudo/dnf, and leave no llama-server running (`ss -ltnp | grep 9931`).

---

## Polish after live tests (Nov 1)

Work branch: `phase5-b1-work`. Live listening now transcribes every turn correctly; the
remaining problems are output clipping, accidental LLM commands, wrong mute replies, stray
template fragments, and the over-loosened guard.

| Part | What | Status |
|------|------|--------|
| Part 1 | Normaliser never clips: cap gain by the true raw peak | DONE (code + tests) |
| Part 2 | Optional `MIC_SOURCE_VOLUME` applied at chat / voice-test start | DONE (code + tests; `.env` set to 0.21) |
| Part 3 | Slash commands never reach the LLM; raw-line logging | DONE (code + tests) |
| Part 4 | mute/unmute explicit + code-built replies; timer honesty; intent guard | DONE (code + tests) |
| Part 5 | Stop sequences + reply cleaner for `<` fragments and repeated sentences | DONE (code + tests) |
| Part 6 | Restore the strict one-signal guard | DONE (code + tests) |
| Part 7 | Full pytest, docs, commits | DONE |

### Live facts (12:24–12:31 session, mic source volume 0.21)

- 8 voice turns transcribed correctly; guard scores: avg -0.089…-0.343, no_speech ≤ 0.10,
  compression ≤ 1.233.
- Raw peaks before normalisation: 9630, 11988, 13460, 13473, 16950, 21189, 24392, 27616 —
  no clipping. Yet 7 of 8 logged `after peak=32767/32768`, e.g.
  `before peak=24392 rms=1565 -> after peak=32767 rms=3269 (speech level 973, gain 2.10x)`.
  The gain cap used the 95th-percentile frame peak instead of the true peak.
- `//voice` (stray slash) was sent to the LLM, which answered "Voice is active at the
  current volume setting."; there is no unknown-command guard.
- "Stay silent for 10 seconds." → mute_audio(true) → "Audio is muted for 10 seconds."
  (no timer exists). "mute unmute" → mute_audio(false) → "Audio is now unmuted."
- "What is your name? What is your name?" → reply with `Ultron.< name is ...` and a
  repeated sentence (106 chars, 29 completion tokens — not a max-token cut).

### Part 1 result (normaliser never clips)

- `assistant/speech/recorder.py`: the gain is now capped by the TRUE raw peak:
  `gain = min(level_gain, 3.0, 0.9 * 32767 / peak)`. A non-clipping recording is never
  attenuated (`gain >= 1.0`); only input above 0.9 full scale is scaled down to 0.9.
  `SAFE_OUTPUT_PEAK` is now 29490 (0.9 FS), and the 95th-percentile peak cap was removed.
- `assistant/interfaces/voice.py`: the clipping warning now says to lower the mic input
  slightly (about 20% below the current value) and shows `wpctl get-volume @DEFAULT_AUDIO_SOURCE@`.
- Exact output on the five requested peaks (speech body RMS 973, as in the live log):

| Raw peak | Output peak | Gain | Clipping |
|----------|-------------|------|----------|
| 9630 | 28890 | 3.000 | no |
| 21189 | 29490 | 1.392 | no |
| 27616 | 29490 | 1.068 | no |
| 32768 (clipped burst) | 29490 | 0.900 | yes |
| 3000 | 9000 | 3.000 | no |

- Tests: `tests/test_mic_level.py` (five peak cases, loud-but-not-clipping stays at gain 1.0).

### Part 2 result (MIC_SOURCE_VOLUME)

- New `assistant/speech/mic_input.py`: validates 0.05–0.6, runs
  `wpctl set-volume @DEFAULT_AUDIO_SOURCE@ <value>` once, logs one line, and never blocks
  startup. Missing wpctl / failures / invalid values are ignored with a log line.
- `assistant/config.py`: `mic_source_volume` reads `MIC_SOURCE_VOLUME` (and `MIC_` is now an
  accepted env prefix).
- `assistant/interfaces/cli.py` (`chat`) and `assistant/doctor.py` (`voice-test`) apply it at
  startup and print one line when configured.
- `.env.example` documents it. The user's `.env` got `MIC_SOURCE_VOLUME=0.21` appended
  (count checked first; file contents never read or printed; `.env` is not committed).
- Tests: `tests/test_mic_input.py` with a fake runner (set, unset, range, missing wpctl,
  failure log, config read).

### Part 3 result (slash commands)

- `assistant/interfaces/cli.py`:
  - `normalize_chat_line()` trims and collapses repeated leading slashes (`//voice` → `/voice`).
  - `command_name()` + `is_unknown_command()` dispatch by exact command name (so `/modelx` is
    unknown, not `/model`).
  - Any remaining slash line prints exactly
    "Unknown command. Available: /model, /thinking, /voice, /mode, /dry-run, /logs, /quit"
    and sends nothing to the LLM.
  - Every slash line, plus bare `voice` / `quit` / `logs`, is logged at INFO as
    `Chat input: <repr>` (secrets redacted).
- Part 3c finding: `_default_enter_pressed()` only polls stdin **while listening** and reads
  exactly one line, so it never leaves a stray Enter and never flushes the next prompt. The
  observed stray "/" was a repeated leading slash (the echo appeared before the prompt, and
  the old `startswith("/...")` checks missed `//voice`), which then fell through to the LLM.
  Characters typed while Piper is speaking stay buffered for the next `input()` by design;
  with the new normalization a leading-slash line is always a command.
- Tests: `tests/test_chat_commands.py` (normalization, dispatch, and an end-to-end CLI run
  proving `/nonsense` never reaches `run_task` and `//voice` starts listening).

### Part 4 result (mute/unmute, timer honesty)

- `assistant/tools/volume.py`: `mute_intent()` (mute / unmute / None),
  `asks_for_timed_mute()` (duration + mute/silence words), and the exact replies
  `WHAT_MUTE_REPLY` and `TIMED_MUTE_REPLY`. `mute_audio` itself was already explicit and
  idempotent (`muted=true/false`), not a toggle.
- `assistant/brain/tool_replies.py`: code-built replies from the real tool result:
  "Audio is now muted.", "Audio is now unmuted.", "Volume is now set to N percent.",
  plus "I couldn't change the audio." / "I couldn't set the volume." on failure.
  `code_reply_for_tool` now covers mute_audio, set_volume, volume_up, volume_down.
- `assistant/brain/agent_loop.py`:
  - a timed mute request is answered before the LLM: no tool runs; the next "yes" mutes now
    (directly, without relying on the model);
  - a mute_audio call with both/neither mute word asks "Do you want to mute or unmute?" and
    runs nothing;
  - the tool's `muted` argument is forced from the spoken word (mute → True, unmute → False),
    so the reply always matches what was done.
- Tests: `tests/test_volume.py` (intents/timer), `tests/test_reply_style.py` (code replies),
  `tests/test_agent_rounds.py` (forced action, ambiguity, timed request, "yes" follow-up).
- Exact replies: "mute" → "Audio is now muted."; "unmute" → "Audio is now unmuted.";
  "mute unmute" → "Do you want to mute or unmute?"; "stay silent for 10 seconds" →
  "I can mute the audio, but I can't unmute it automatically after a time. Should I mute it now?"

### Part 5 result (garbled chat replies)

- The LFM3 GGUF uses `<|im_start|>`, `<|im_end|>`, `<|endoftext|>`, `<|startoftext|>`.
  `assistant/brain/local_client.py` now sends `stop` with those four sequences and raises
  `repeat_penalty` to 1.15. llama-server launch flags are untouched.
- `assistant/brain/reply_style.py`: `clean_model_text()` removes `<|...|>` tokens,
  unterminated `<|...`, `</s>`/`<s>`, and stray `<` fragments, collapses an immediately
  repeated sentence, and is applied in `local_client._extract` and `plain_text`.
- Why the live reply looked broken: the model emitted 29 completion tokens (not a max-token
  cut), but `Ultron.< name` had no space after the period, so the sentence splitter kept both
  sentences. Removing the `<` lets the existing one-sentence shaper drop the repetition.
- Exact cleaned reply for the live bad string:
  "I am Fedora GNOME voice assistant, Ultron."
- Tests: `tests/test_reply_style.py`, `tests/test_local_client.py` (stop sequences, penalty,
  exact bad name string, special tokens, repeated sentence).

### Part 6 result (strict guard restored)

- `assistant/speech/ears.py`: removed the two-signal/extreme logic. The guard now rejects on
  **any one** of: `avg_logprob < -1.0`, `no_speech_prob > 0.60`, `compression_ratio > 2.4`,
  plus the real-word and repetition-loop rules. This supersedes the loosened rule from the
  previous session.
- `assistant/config.py` / `.env.example`: only the three limits remain configurable
  (`WHISPER_MIN_AVG_LOGPROB`, `WHISPER_MAX_NO_SPEECH_PROB`, `WHISPER_MAX_COMPRESSION_RATIO`).
- Verdicts:
  - six good clips (−0.141…−0.650 / 0.012…0.066 / 0.385…0.814) → accepted;
  - eight live turns (−0.089…−0.343 / ≤0.101 / ≤1.233) → accepted;
  - "Where is the MMAS folder?" (−1.087 / 0.065 / 0.76) → **REJECTED** (`avg_logprob=-1.09`);
  - "Turn, board, volume to, cut, triple, percent" (avg −1.4) → rejected;
  - "mute, mute, unmute" → rejected by the repetition loop;
  - "List files, see in my down download for 4-fold editor" (avg −1.4) → rejected.
- Tests: `tests/test_ears.py` updated (strict default, configurable limits, good clips +
  live scores accepted, MMAS attempt rejected).

## Head state (polish after live tests)

- Branch `phase5-b1-work`, local commits only: Part 1 `5010879`, Part 2 `a2f7d0c`,
  Part 3 `d699ffd`, Part 4 `5cd924f`, Part 5 `0a583ab`, Part 6 `021987e`,
  Part 4 follow-up `b3019a9`, Part 7 `4d35da1`, handoff docs `6853561` and `0816c42`.
- Latest pytest: **177 passed**. No llama-server running (`ss -ltnp | grep 9931` empty).
- Working tree clean; nothing pushed; the remote and git config were not touched.
- `.env` contains `MIC_SOURCE_VOLUME=0.21` (appended without reading/printing; not committed).
- To catch up: read this file, `git switch phase5-b1-work`, run
  `.venv/bin/python -m pytest -q`.

## NEXT STEP (polish after live tests)

Live tests (the coding tool cannot use the microphone). Run and report each:

1. `.venv/bin/python voice-test` — expect `[Mic] Mic source volume set to 0.21`, then say
   "open firefox and list my downloads"; the transcript must be complete.
2. `.venv/bin/python main.py chat`:
   - **REAL silence test**: `/voice`, then say nothing for 10 seconds. Expect the spoken
     "I didn't hear anything.", no transcript, no tool call.
   - `/voice` "mute" → "Audio is now muted."; "/voice" "unmute" → "Audio is now unmuted."
   - `/voice` "mute unmute" → "Do you want to mute or unmute?" and no tool call.
   - `/voice` "stay silent for 10 seconds" → the honest timer reply; say "yes" → it mutes
     now (no timer, no automatic unmute).
   - Type `//voice` → it must start listening, not answer as text.
   - Type `/nonsense` → "Unknown command. Available: …" and nothing else.
   - `/voice` "what is your name?" → one clean sentence, no "<" and no repeated sentence.
   - `/voice` "turn volume to 30 percent" → "Volume is now set to 30 percent."
   - Check `logs/assistant.log`: every `Mic level` line must show `after peak` ≤ ~29490.
3. If anything fails, keep the newest `workspace/voice_debug/attempt_*.json` and the log tail.


## Listening fix (Nov 1)

Work branch: `phase5-b1-work`. Live listening is broken: attempts are missed, cut off,
or rejected by the "say it again" guard.

| Part | What | Status |
|------|------|--------|
| Part 1 | Evidence from `logs/assistant.log`, `logs/audit.jsonl` and the newest `workspace/voice_input.wav` | DONE (see below) |
| Part 2 | Robust listening: 2 s end silence, hysteresis, adaptive levels, Enter-to-finish, 10 s start timeout, 400 ms pre-roll, streaming fixed mode, debug captures | DONE (code + tests) |
| Part 3 | Guard: log scores, reject only on 2 bad signals or 1 extreme, configurable limits | DONE (code + tests) |
| Part 4 | Offline tests for 9 listening cases | DONE (10 tests + debug, 150 total pytest) |

### Part 1 evidence (live session 11:04–11:10, today)

All attempts below are from `logs/assistant.log`; the guard does not log its scores yet,
so the last row was re-scored by running the saved newest WAV through production Whisper.

| Time | Captured / speech | Stop reason | Whisper text | Guard |
|------|-------------------|-------------|--------------|-------|
| 11:04:14 | 2.0 s / 0.6 s | silence (peak 32768, rms 24520) | — VAD no speech | not reached |
| 11:04:29 | 5.2 s / 3.2 s | silence | not logged | REJECTED |
| 11:04:40 | 6.1 s / 3.9 s | silence | "This is my FFI, I'll just hit him and download it." | accepted (wrong) |
| 11:04:58 | 14.3 s / 6.3 s | silence | "The list of my FF5 files is in my mail-down order folder" | accepted (wrong) |
| 11:06:59 | 2.0 s / 0.6 s | silence (peak 32768, rms 22468) | — VAD no speech | not reached |
| 11:07:08 | 6.4 s / 3.5 s | silence | not logged | REJECTED |
| 11:07:22 | 6.2 s / 3.5 s | silence | not logged | REJECTED |
| 11:07:42 | 4.8 s / 1.9 s | silence | "Turn the volume up" | accepted |
| 11:08:04 | 5.8 s / 2.6 s | silence | not logged | REJECTED |
| 11:08:21 | 10.7 s / 4.5 s | silence | not logged | REJECTED |
| 11:08:32 | 5.3 s / 2.2 s | silence | not logged | REJECTED |
| 11:08:48 | 8.0 s / 0.2 s | **timeout** | — never transcribed | not reached |
| 11:08:55 | 8.0 s / 0.3 s | **timeout** | — never transcribed | not reached |
| 11:09:03 | 8.0 s / 0.7 s | **timeout** | — never transcribed | not reached |
| 11:09:10 | 6.4 s / 1.6 s | silence | not logged | REJECTED |
| 11:09:21 | 3.3 s / 0.8 s | silence | "Hey!" | accepted |
| 11:09:32 | 8.4 s / 4.1 s | silence | "List my four letters in download order." | accepted (wrong) |
| 11:09:50 | 8.4 s / 4.0 s | silence | "Where is the MMAS folder?" | REJECTED |

Re-scored newest WAV (`workspace/voice_input.wav`, copied to `/tmp/real_attempt_1109.wav`):
5.98 s, peak 24494, rms 3390, `avg_logprob=-1.087`, `no_speech_prob=0.065`,
`compression_ratio=0.76` → **rejected by only 0.087 log-prob**. That is genuine speech.

Which causes the evidence supports:

1. **Cause 3 — the guard rejects real speech: CONFIRMED.** 8 attempts were rejected; the
   measurable one is just past `-1.0`. The guard was tuned on six clean clips only.
2. **Cause 2 — start detection: CONFIRMED.** Three attempts ended by the 8 s start timeout
   with 0.2–0.7 s of "speech-like" frames but never 200 ms consecutive; two more recorded a
   full-scale thump (peak 32768, rms 22468–24520) as "speech" and Whisper then found no
   speech. The start threshold and calibration are too easily polluted / too high.
3. **Cause 1 — end silence: LIKELY.** Long sentences stopped at a "silence" after 1 s and
   some recordings are short relative to the sentence (5.2–6.4 s with 2.6–3.9 s speech).
4. **Cause 4 — pre-roll/trimming: NOT EVIDENCED.** The first word does not show a consistent
   loss pattern; the bigger problems are start detection and guard rejection.

### Part 2 result (listening robustness)

- **Found and fixed a structural bug:** `VoiceController.record_auto()` started
  `PwRecordSource` and `record_until_silence()` started it a second time. Two `pw-record`
  processes shared one queue; the first was never stopped. The recorder now owns start/stop.
- `assistant/speech/recorder.py`:
  - end of speech: default `silence_ms=2000`, hysteresis (end threshold is much lower than
    start), relative to the speaker's own level (`END_SPEAKER_FRACTION=0.18`) and the room
    noise (`1.8x`); no stop before `MIN_SPEECH_BEFORE_SILENCE_STOP_MS=1500`.
  - start: `ABS_START_RMS=280`, capped at `MAX_START_RMS=500`, so a soft word (5x quieter)
    triggers; the noise floor is the 25th percentile of the first 200 ms, and saturated
    bursts (crest < 2.2, e.g. the mic wake-up thump) never count as voice.
  - pre-roll 400 ms; start timeout default 10 s.
  - Enter finishes the capture and is consumed from the terminal; Enter before speech
    cancels; Ctrl+C still cancels only the capture.
  - `record_for_duration()` for fixed mode over the same streaming source.
  - every `Recording` now carries `debug_pcm` + a level `trace`.
- `assistant/config.py`: `VOICE_SILENCE_MS` default 2000, `VOICE_START_TIMEOUT_S` default 10.
- `assistant/interfaces/voice.py`:
  - fixed mode rewritten to stream (no `pw-record <file>` + SIGINT), so the whole window is
    kept.
  - every attempt saves `workspace/voice_debug/attempt_<ts>.wav` (raw) and `.json`
    (reason, duration, speech, thresholds, per-100 ms levels, transcript, guard scores);
    newest 10 kept. One INFO line per attempt with the same summary.
- `.env.example`: documents all voice settings.
- Tests updated: `tests/test_voice_auto.py`, `tests/test_mic_level.py`.

### Part 3 result (guard tuning)

- `assistant/speech/ears.py`: `GuardLimits` with configurable limits; every transcript logs
  `Ears confidence: min_avg_logprob=… max_no_speech=… max_compression=… verdict=… text=…`.
- New rule: reject only on **two** bad signals or **one** extreme value, or the existing
  real-word / repetition-loop text rules.
  Defaults: bad at `avg_logprob < -1.0`, `no_speech_prob > 0.60`,
  `compression_ratio > 2.4`; extreme at `-1.5`, `0.85`, `3.0`.
- Verification: all six `/tmp/cmp2_*.wav` clips → `verdict=ok`. The real rejected attempt
  (`/tmp/real_attempt_1109.wav`, `-1.0866 / 0.0647 / 0.7576`) → `verdict=ok`.
  Old rule rejected it (single signal past `-1.0`).
- `.env.example`: `WHISPER_MIN_AVG_LOGPROB`, `WHISPER_MAX_NO_SPEECH_PROB`,
  `WHISPER_MAX_COMPRESSION_RATIO`, `WHISPER_EXTREME_*`.

### Part 4 result (offline listening tests)

`tests/test_listening_reliability.py` adds 10 tests: the 9 required cases plus the debug
capture. Real-clip tests skip with a clear message when `/tmp/cmp2_*.wav` is missing; the
other cases use synthetic speech-like audio. Measured before/after by loading the old
recorder module from commit `c509c62` and replaying the same inputs:

| # | Case | Before (old build) | After (new build) |
|---|------|--------------------|-------------------|
| 1 | long sentence (clip 4 + noise) | 1 recording, 6.94 s, text correct | 1 recording, 7.98 s, text correct |
| 2 | `cmp2_1` + 1.5 s + `cmp2_2` | saved 5.54 s, **second sentence cut** | saved 12.08 s, **both sentences** transcribed |
| 3 | 1.9 s pause inside a sentence | old 1 s default: saved 1.8 s, second half lost | 2 s default: saved 5.0 s, both halves kept |
| 4 | soft start (0.8 s at 0.2x) | threshold 660 > 600: start delayed to the loud part | detected at 0.5 s (`speech_start_ms=500`), full soft part kept |
| 5 | low room noise before/after | detected | detected; noise ≈90, start threshold 280 |
| 6 | 100 ms full-scale click first | ignored (warmup) | ignored (burst rule); speech start ≥ 1.1 s |
| 7 | noise only for the start timeout | timeout 1 s, nothing sent | timeout 1 s, "I didn't hear anything.", nothing sent |
| 8 | Enter mid-speech | no Enter support: recorded until source end (6.3 s) | ends on Enter (~2.4 s), keeps speech; hint printed once |
| 9 | fixed 2.5 s window | old file+SIGINT path could keep only the first word | streams 2.5 s and keeps the last 0.5 s loud content |

Debug captures are also tested: the JSON gets transcript + guard scores, only the newest
10 attempts are kept, and each attempt writes exactly one INFO line.

## Head state (listening fix)

- Branch `phase5-b1-work`; commits: Part 2 `f4d15e9`, Part 3 `228c1c2`, Part 4 committed
  after this file. Nothing pushed; the remote and git config were not touched.
- Latest pytest: `150 passed`. No llama-server running (`ss -ltnp | grep 9931` empty).
- To catch up: read this file, `git switch phase5-b1-work`, run
  `.venv/bin/python -m pytest -q`.

## NEXT STEP (listening fix)

Live voice tests (the coding tool cannot speak into the mic). Run these and report the
result of each:

1. `.venv/bin/python main.py voice-test` — say "open firefox and list my downloads" normally;
   it must capture the whole sentence and print the transcript. Press Enter to finish early.
2. `.venv/bin/python main.py chat`, then `/voice`:
   - "list files in my downloads folder" (pause ~1 s between words) → full transcript and a
     real item count.
   - Stay silent for 10 s → "I didn't hear anything." and nothing is sent to Whisper.
   - Mumble → "I didn't catch that, please say it again." only if the evidence is really bad.
3. If a command is still cut or missed, keep `workspace/voice_debug/attempt_*.json` and
   `logs/assistant.log`; the JSON now records thresholds, levels, transcript and guard scores.




## Status

| Task | Status |
|------|--------|
| Task 1 | done per user, not yet verified live (voice tests pending) |
| Task 2 | done per user, not yet verified live (voice tests pending) |
| Task 3 | done per user, not yet verified live (voice tests pending) |
| Task 4 (list_files reply + lead-in guard + regression test) | DONE (code + tests) |

Tasks 1-3 were not audited or changed. No issues were found that need a code change.

## Follow-up fixes (Nov 1)

Work branch: `phase5-b1-work`. Ignore the old NEXT STEP below; this section supersedes it.

| Part | What | Status |
|------|------|--------|
| Part 1 | Diagnose the garbled transcript: A/B the six /tmp/cmp2_*.wav files through plain Whisper, production ears, production normalisation, and record_until_silence | DONE (see findings below) |
| Part 2 | Fix the root cause only + regression test, keep base.en | DONE (code + tests, commit pending) |
| Part 3 | "Say it again" confidence guard + set_volume number guard | DONE (commit 434fcac) |
| Part 4 | list_files bug: log tool args, never claim "no items" wrongly, robust spoken folder names | DONE (code + tests, commit pending) |
| Part 5 | PyAV safety: no file paths to WhisperModel.transcribe, add guard test | DONE (code + tests, commit pending) |

### Part 1 findings (diagnosis only)

- Production decode call: `assistant/speech/ears.py:167` (`model.transcribe(audio, **decode_options)`).
  Audio is a float32 numpy array from `load_wav_16k_mono` (line 140). Options: language="en",
  beam_size=1, vad_filter=True, condition_on_previous_text=False, without_timestamps=True.
  temperature / vad_parameters / initial_prompt / prompt / word_timestamps are NOT passed
  (library defaults). `hotwords` used to be passed at old line 195 as
  `config.whisper_hotwords or build_hotwords()`.
- The auto hotword list was built in `build_hotwords()` from `config/apps.yaml` + TOOL_WORDS
  (old lines 33-65) and was used on every call when `WHISPER_HOTWORDS` was blank (it is blank
  in `.env`). On unclear audio faster-whisper echoes that comma-separated list into the text.
  Live log proof: "List files, see in my down download for 4-fold editor",
  "Turn, board, volume to, cut, triple, percent", "mute, mute, unmute" — all are fragments of
  the hotword list. Replay proof: production on cmp2_1 gave `list, list files...`; the same
  call with hotwords off gave `list files...`. VAD on/off made no difference.
- Mic normalisation/gain (Task 3, `voice.py:360-381`, `normalise_mic_level` in
  `recorder.py:520`) and trimming (Task 2, `recorder.py:297-466`) did NOT damage any of the
  six clips: plain, normalised+plain and recorder+plain were all semantically correct.
- Only replay artifact: feeding a file with synthetic *pure-zero* lead-in silence makes
  Whisper add one duplicate word on cmp2_1 (`list list files...`). It keeps the meaning; the
  live mic never produces digital zeros. Not changed.
- A/B before fix (exact text, case/punctuation ignored): plain 4/6, production 4/6,
  hotwords-off 4/6, normalised+plain 4/6, recorder+plain 4/6. The two "misses" are only
  `30%` vs `thirty percent` and singular `download` vs `downloads` on every variant, i.e.
  not caused by any stage.

### Part 2 result

- `assistant/speech/ears.py`: deleted the auto-built hotword list (`TOOL_WORDS`,
  `load_app_names`, `build_hotwords`). `transcribe()` now builds `decode_options` and adds
  `hotwords` only when `WHISPER_HOTWORDS` is non-empty. No `initial_prompt`/`prompt` is ever
  passed. Docs updated in `.env.example`.
- `tests/test_ears.py`: default call has no hotwords/initial_prompt/prompt; explicit
  `WHISPER_HOTWORDS` still works; decoded settings test still checks phase 3 values.
- A/B after fix, production path: 4/6 exact (same benign wording) and 6/6 semantically
  correct (`30%` == `thirty percent`, `download` == `downloads`); the hotword fragments are
  gone. 113 tests passed.

### Part 3 result ("say it again" + set_volume number guard)

- `assistant/speech/ears.py`: `transcript_is_unreliable()` rejects a transcript when
  `avg_logprob < -1.0`, `no_speech_prob > 0.60`, `compression_ratio > 2.4`, there is no real
  word, or one word is at least half of a short transcript (repetition loop). The six good
  clips measured -0.14..-0.65 / 0.01..0.07 / 0.38..0.81, so they pass with a wide margin.
  Rejected transcripts return `UNCLEAR_SPEECH` and never reach the LLM or a tool.
- `assistant/interfaces/cli.py`: on a voice turn the CLI now speaks the
  "I didn't catch that, please say it again." message.
- `assistant/tools/volume.py`: `numbers_in_text()`/`percent_is_mentioned()` read digits and
  English number words (incl. "fifty-five", "one hundred").
- `assistant/brain/agent_loop.py`: `set_volume` runs only if its exact percent appears in
  the transcript; otherwise it returns "What volume should I set? Say a number from 0 to 100."
  before executing any tool and without a second LLM turn.

### Part 4 result (list_files false "no items")

- Real live cause (from `logs/audit.jsonl` line 243): request
  "List files, see in my down download for 4-fold editor", arguments
  `{"directory": "~/Downloads", "pattern": "*down*"}`. The folder was fine; the model's
  invented wildcard matched 0 of 43 items, so the tool returned `count: 0`.
- `assistant/tools/files.py`: a non-default pattern that hides everything now falls back to
  listing the folder itself; spoken aliases map download/downloads/documents/desktop/home and
  "in my … folder"; bare names always resolve under `$HOME`.
- `assistant/brain/tool_replies.py`: a failed list_files always speaks
  "I couldn't find that folder."; "There are no items…" is only possible from a real empty
  listing; `claims_an_empty_folder()` catches a model-worded empty claim.
- `assistant/brain/agent_loop.py`: `last_tool` now records failed calls too, so the code reply
  replaces invented wording.
- `assistant/tools/registry.py`: one INFO log line per tool call with name + redacted args.

### Part 5 result (PyAV safety)

- `tests/test_ears.py::test_whisper_never_receives_a_file_path_or_file_object` asserts the
  model gets a contiguous float32 1-D numpy array, never a `str`/`Path`. Production already
  converts with `load_wav_16k_mono()`; no file path or file object reaches `transcribe()`.
- Nothing was installed, upgraded or downgraded.

### Final A/B (production `WhisperEars.transcribe`, after all fixes)

| File | Transcript | Semantic match |
|------|------------|----------------|
| cmp2_0 | Turn volume to 30%. | yes |
| cmp2_1 | list files in my download folder | yes |
| cmp2_2 | Open Firefox. | yes |
| cmp2_3 | Mute. | yes |
| cmp2_4 | Open Firefox and list my downloads. | yes |
| cmp2_5 | Turn the volume up | yes |

Exact text ignoring case/punctuation is 4/6 only because Whisper writes `30%` instead of
`thirty percent` and drops the plural in `download`; both are accepted as the same command.



## Head state

- Local branch for this work: `phase5-b1-work` (from `main` @ 9d95911).
- Follow-up commits (local only, nothing pushed): Part 2 `1fca0a2`, Part 3 `434fcac`,
  Part 4 and Part 5 committed after this file was written.
- Working tree is clean after the last follow-up commit; the remote was never touched.
- To catch up: read this file, then `git switch phase5-b1-work` and run
  `.venv/bin/python -m pytest -q` (expect 132 passed).

## Task 4 result

- `assistant/brain/tool_replies.py`: reply is built in code from the tool result.
  - 0 items: "There are no items in your Downloads folder."
  - 1 item: "There is 1 item in your Downloads folder."
  - N items: "There are N items in your Downloads folder."
  - General guard: a number-free opener ("Here are …", "Sure …") that is at most 14 words is
    replaced by a per-tool template (`fallback_reply`).
- `assistant/brain/agent_loop.py`: `_finalize_reply` uses the code-built reply first, then the
  lead-in guard (unchanged from the earlier session; verified by tests).
- `tests/test_reply_style.py`: the mid-edit end-to-end test was checked and passes. Added:
  - `test_list_files_reply_uses_the_real_count_and_folder` (0 / 1 / 43 wording),
  - `test_list_files_reply_is_skipped_for_a_failed_result`,
  - `test_bad_downloads_lead_in_is_replaced_by_a_code_reply` using the exact bad reply
    "Here are the first items in your download folder.",
  - `test_lead_in_fallback_is_a_plain_sentence_per_tool`.

## Files changed in Phase 5 Part B1

- `assistant/brain/tool_replies.py` (new in the snapshot; empty-folder sentence finished now).
- `assistant/brain/agent_loop.py` (snapshot; no change needed this session).
- `tests/test_reply_style.py` (regression tests added).
- `docs/PROGRESS.md` (this file), `AGENTS.md` (one-line pointer added at the top).

## Latest pytest

`132 passed` (0 failed) after the follow-up fixes. Before this session: 114 passed.

## Open problems

- Tasks 1-4 have not been verified with a real microphone/speakers yet.
- No code problems found in Tasks 1-3; nothing was changed there.
- No llama-server is running now (`ss -ltnp | grep 9931` shows no listener).
- Report only: normalising a quiet clip can boost up to 3x and hit full scale (cmp2_3
  "mute" reached peak 32768 after gain 3.00) without hurting recognition. Live mic peaks are
  already near clipping (24k-32k); lowering the mic input is still worth doing.
- Report only: PyAV 19 breaks faster-whisper 1.2.1 when a file path is passed. Nothing was
  pinned or downgraded; the user decides whether to pin av<19.

## NEXT STEP - how to resume

Parts 1-5 are done. The next step is the user's live voice test (do not run interactive
voice commands from the coding tool), then review the follow-up commits:

1. `.venv/bin/python main.py voice-test` — say "list files in my downloads folder"; the
   transcript must have no comma-separated word-list fragments.
2. `.venv/bin/python main.py chat`, then `/voice`:
   - "list files in my Downloads folder" → "There are N items in your Downloads folder."
   - "list files in a folder that does not exist" → "I couldn't find that folder."
   - "set the volume" (no number) → "What volume should I set? Say a number from 0 to 100."
   - "turn volume to thirty percent" → the volume tool runs.
3. Mumble or say a partial sentence and confirm "I didn't catch that, please say it again."
   is spoken and no tool runs.
4. Do not start Phase 5 Part C without the user's approval.
