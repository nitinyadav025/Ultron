# SESSION STATE — Phase 8, saved 2026-11-05

## Repository

| Item | Value |
|---|---|
| Working folder | `/home/nitinyadav/Downloads/ULTRON` (`pwd` verified) |
| Branch | `phase8-eyes-hands-ears` |
| Base | `phase7-llm-brain-wayland` @ `242a57f` |
| Phase 8 code/docs commit | **`aad7d29`** — 27 files, 3798 insertions, 98 deletions |
| Handoff commit | the commit that adds `left-work-phase8/` (after `aad7d29`) |
| Live-GUI fix commit | the next local commit: `apps.py` 15-char `comm` fix, Flatpak `ps` capture, 3 tests, docs |
| Remote | nothing pushed; `origin` untouched |
| `.env` | git-ignored, edited with `sed`, never printed/staged |

## Completion status

| Part | Status |
|---|---|
| 1. agent-desktop + accessibility bus + Chromium flag | **CODE DONE**; agent-desktop 1.2.0 installed and live-probed |
| 2. 3-layer eyes + ScreenWatcher | **CODE DONE**; AT-SPI + watcher live-probed; OCR live blocked (tesseract missing) |
| 3. click_element / read_screen / focus_and_type | **CODE DONE + UNIT TESTED**; live clicks/typing UNVERIFIED |
| 4. small.en + fast decode + dynamic hotwords + repairs | **CODE DONE + MEASURED**; latency missed the 0.55–0.75 s target |
| 5. gio launch + alias table + switch/focus | **CODE DONE + LIVE VERIFIED** for calculator, text editor, files, terminal and settings; Lutris starts but shows no window |
| 6. tests + benchmark + docs + cleanup | **DONE** (540 passed, 37 Phase 8 tests) |

**Coding work remaining: none known.** A second session verified the real `gio launch` path
live and fixed two close-app bugs it exposed (see the live-GUI section below). The remaining
work is the live microphone / voice / click / typing pass and the post-live docs update.

## Verification evidence (actually run)

```text
.venv/bin/python -m pytest -q
540 passed in 53.50 s

.venv/bin/python main.py benchmark
25/25 passed, failed 0; average 1.480 ms (mocked OS/LLM)

.venv/bin/python -m compileall -q assistant main.py scripts tests/test_phase8_eyes_hands_ears.py
OK

git diff --check
clean

.venv/bin/python -m pytest tests/test_phase8_eyes_hands_ears.py -q
37 passed
```

Live probes (no clicking/typing):

- `read_screen()` → `focused_window=Downloads`, `focused_app=files`, **14 AT-SPI elements**,
  visible UI `@e1:button:"Back" ... @e12:toggle:"Search Everywhere"`.
- `ScreenWatcher` at 0.5 s for 2 s → 14 elements refreshed; thread stopped; no helper left.
- `agent-desktop list-windows` → real Nautilus window (`Downloads`, pid 16814).
- `agent_desktop_snapshot()` → **21 elements in 337 ms**.
- `/usr/bin/python3 -c "import gi; ... Atspi"` → `Atspi OK`; `pyatspi OK`.
- `gsettings get org.gnome.shell introspect` → **No such key** (GNOME Shell 50.5).
- `gdbus ... GetWindows` → **AccessDenied: GetWindows is not allowed**.
- `command -v tesseract` → **missing**.
- Real `open_app` → `gio launch` → Calculator, Text Editor, Files, Terminal and Settings:
  each window appeared, AT-SPI returned **10–38 elements**, and the fixed `close_app`
  removed it (verified by polling `agent-desktop list-windows`).
- `open_app("lutris")` → the Flatpak process started (`flatpak ps` showed
  `net.lutris.Lutris`) but **no window appeared in 40 s**; the fixed `close_app` stopped the
  sandbox. The spoken Lutris step stays UNVERIFIED.
- Process check at the end: no `llama-server` on 9931, no `ydotoold`, no `atspi_helper.py`,
  no `agent-desktop`, no test app left running.

### small.en measured on the 10 saved clips (production options, env WHISPER_* unset)

Model load **0.91 s**.

| Clip | Saved base.en text | small.en time | small.en result |
|---|---|---|---|
| 134409 | `Unused.` | 1.85 s | rejected `avg_logprob=-1.40` (same as before) |
| 134437 | `unmute` | 1.99 s | `Unmute.` |
| 134518 | `Stay silent for 10 seconds.` | 1.91 s | exact |
| 134532 | `Yes.` | 1.75 s | exact |
| 134543 | `Can you unmute the voice please?` | 2.03 s | exact |
| 134606 | `list files in my download folder` | 2.03 s | exact |
| 134642 | `What is your name?` | 2.08 s | exact |
| 134700 | `Turn the volume to 90%.` | 1.90 s | exact |
| 135201 (30 s fan) | `""` | 0.07 s | rejected `no_real_word` (correct) |
| 135223 | `mute unmute` | 1.84 s | `Mute on mute.` → rejected `repetition_loop` |

- 7 of 8 real speech clips pass (base.en passed 8/8).
- **Measured latency is 1.75–2.08 s per clip, not the 0.55–0.75 s target.** The requested
  settings were kept; if speed matters, set `WHISPER_MODEL=base.en` in `.env`.

## Live GUI verification and two close-app fixes (second session, 2026-11-05)

The real launcher path was exercised on the live desktop (no microphone, no key presses):

| App | Window observed | AT-SPI | `close_app` |
|---|---|---|---|
| Calculator | `Calculator` | 38 elements | closed, window gone |
| Text Editor | `agentskills.txt … - Text Editor` | 10 elements | closed, window gone |
| Files | `Home` | 14 elements | closed, window gone |
| Terminal | `nitinyadav@Sasuke:~` | 13 elements | closed, window gone |
| Settings | `Settings` | 32 elements | closed, window gone |
| Lutris (Flatpak) | **none in 40 s** | 0 (no window) | `flatpak kill` closed the sandbox |

**Two live bugs found and fixed:**

1. **`close_app` could not close long process names.** Linux truncates `/proc/<pid>/comm` to
   15 characters and `pgrep -x` refuses a longer pattern, so Calculator/Text Editor/Settings
   reported `running=False, closed=False` and stayed open. `_process_names_for()` now appends
   the 15-character alias. Live PIDs: `gnome-calculato` 15843, `gnome-text-edit` 15943,
   `gnome-control-c` 16169.
2. **`flatpak ps` output was discarded.** `_default_flatpak_runner()` used
   `stdout/stderr=DEVNULL`, so `_flatpak_running_apps()` always returned an empty set and
   `close_app("lutris")` reported `running=False` while `flatpak ps` showed
   `net.lutris.Lutris`. It now captures the short-lived `flatpak ps`/`flatpak kill` output;
   the all-DEVNULL rule still applies to detached GUI/Electron launches.

New tests: `test_process_names_include_the_linux_15_char_comm_alias`,
`test_close_app_finds_a_process_name_truncated_to_15_chars`,
`test_default_flatpak_runner_captures_ps_output`. Phase 8 module **37 passed**; full suite
**540 passed**; benchmark **25/25** at **1.480 ms** (mocked).

**Lutris caveat:** `open_app("lutris")` starts the Flatpak and the fixed `close_app` stops it,
but no Lutris window appeared within 40 s and a foreground `flatpak run` produced no output.
This is a Lutris/runtime issue on this machine, not a `gio launch` failure; the spoken
"open lutris" step remains UNVERIFIED.

## Known caveats / traps

1. **GNOME 50.5 introspection is gone.** `org.gnome.shell introspect` does not exist and
   `GetWindows` is AccessDenied. AT-SPI is the working eye. `ensure_accessibility()` reports
   `introspect: false` honestly.
2. **`tesseract` is not installed.** Live OCR is UNVERIFIED; the parser and click-from-OCR
   path are unit-tested. Install command (needs Nitin/sudo): `sudo dnf install -y tesseract`.
3. **Environment shadows `.env`.** The first Phase 8 session inherited
   `WHISPER_MODEL=base.en` and `WHISPER_MODEL_DIR=models/faster-whisper-base.en`; the second
   session exports small.en. `load_config` gives environment variables precedence over `.env`
   (existing design). `tests/conftest.py` pops both. If an old shell still exports base.en:
   `env -u WHISPER_MODEL -u WHISPER_MODEL_DIR .venv/bin/python main.py listen`.
4. **ydotool 1.0.4 syntax corrected.** The real command is
   `ydotool mousemove --absolute <x> <y>` (positional). `-x/-y` does not exist; the
   implementation uses the real syntax.
5. **Tier 3 browser click is UNVERIFIED.** `Ctrl+F → paste → Esc → Enter` is implemented as
   requested but its live effect is unproven.
6. **Continuous watching never screenshots.** OCR runs only for an explicit
   `read_screen`/`click_element`, keeping the 1.2 s refresh cheap and private.
7. **GTK4 bounds:** SCREEN `x/y` are 0 for children; the helper adds WINDOW coords to the
   frame origin. Verified on the Back button (`239,0 34×34`, centre `256,17`).
8. **Refs are renumbered `@e1..@eN`** after ranking; the AT-SPI helper cache matches them.
9. **Process names are truncated to 15 chars.** `pgrep -x` cannot match a longer name;
   `_process_names_for()` now returns both the full name and the truncated alias so
   `close_app` works for `gnome-calculator`, `gnome-text-editor` and `gnome-control-center`.
10. **`flatpak ps` must be captured.** Its stdout is the running-app list; a DEVNULL runner
    silently breaks `close_app` for Discord/Spotify/Steam/Lutris/VLC. The runner now captures
    only the short-lived CLI commands; detached GUI launches stay all-DEVNULL.

## What is left — exact live checklist for Nitin

Run from `/home/nitinyadav/Downloads/ULTRON`:

0. Optional OCR (sudo; coding tool did not run it):
   `sudo dnf install -y tesseract` then `command -v tesseract`.
1. `.venv/bin/python main.py listen` (the second session's environment already exports
   small.en; if an older shell exports base.en, prefix
   `env -u WHISPER_MODEL -u WHISPER_MODEL_DIR`). Expect
   `Ears: ready — faster-whisper small.en ready (CPU int8)` and
   `Screen watcher started (1.2s).` in `logs/assistant.log`.
2. Say **“Ultron, what is on my screen”** → active window/site + visible buttons;
   `logs/assistant.log` shows `Fast path: used … read_screen`.
3. Open YouTube in Firefox, say **“Ultron, click on show more”** and
   **“Ultron, expand the show more section”** → description expands (Tier 1/2, Tier 3 only
   if needed).
4. Say **“Ultron, type f1 highlights in the search box”** → field focused + text pasted.
5. Open Brave, confirm a YouTube button/link appears in `[Visible UI: ...]`, repeat step 3
   (proves `--force-renderer-accessibility`).
6. (launcher already live-verified; only spoken routing remains) Say **“Ultron, open
   terminal/settings/files/calculator/text editor/lutris”** → each opens or raises through
   `gio launch`; no “I couldn't find an app”. Lutris starts as a Flatpak but showed no window
   in the live probe.
7. Say **“Ultron, open YouTube”** twice → existing window/tab is raised, no duplicate.
8. Say **“Ultron is stop listening”** → stops; then verify:
   ```sh
   ss -ltnp | grep 9931 || echo "no llama-server"
   pgrep -a ydotoold || echo "no ydotoold"
   pgrep -af atspi_helper.py | grep -v pgrep || echo "no helper"
   ```

If a step fails, keep the tail of `logs/assistant.log` and the newest
`workspace/voice_debug/attempt_*` and report the exact `Fast path`/`Tool call`/`Ears` lines.

## Exact resume procedure for the next coding session

```sh
cd /home/nitinyadav/Downloads/ULTRON
git branch --show-current          # expect phase8-eyes-hands-ears
git log --oneline -4               # expect the live-GUI fix + aad7d29 + the handoff commit
git status --short                 # expect clean
.venv/bin/python -m pytest -q      # expect 540 passed
.venv/bin/python main.py benchmark # expect 25/25, ~1.5 ms
```

Then:
1. Read `left-work-phase8/ORIGINAL_PROMPT.md`, this file, then
   `docs/ULTRON_PROJECT_BIBLE.md` §0/§6B and `docs/PROGRESS.md` top section.
2. Ask Nitin for the live results of items 1–5 and 7–8 above (item 6's launcher path is
   verified; only the spoken routing and the Lutris window remain).
3. If he reports a failure, read the exact log lines first (honesty rule: no guessing),
   fix only that path, add a test, run the full suite, update docs, commit locally.
4. If all live tests pass, update `docs/PROGRESS.md` / the Bible to mark the UNVERIFIED
   items verified, run final pytest + benchmark, and commit. Do not push.

## Files created / changed by Phase 8 (commit `aad7d29`)

**New**
- `assistant/vision/__init__.py`
- `assistant/vision/atspi_helper.py`
- `assistant/vision/screen_watcher.py`
- `assistant/tools/screen_actions.py`
- `tests/test_phase8_eyes_hands_ears.py`
- `left-work-phase8/ORIGINAL_PROMPT.md`, `README.md`, `SESSION_STATE.md`

**Changed**
- `assistant/config.py`, `assistant/brain/context.py`,
  `assistant/brain/fast_path.py`, `assistant/brain/prompts.py`,
  `assistant/brain/tool_replies.py`
- `assistant/tools/apps.py`, `assistant/tools/web.py`,
  `assistant/tools/desktop_control.py`, `assistant/tools/registry.py`,
  `config/apps.yaml`
- `assistant/speech/ears.py`, `assistant/speech/wake.py`
- `assistant/interfaces/cli.py`, `assistant/interfaces/gui.py`,
  `assistant/interfaces/listen.py`
- `tests/conftest.py`, `tests/test_ears.py`,
  `tests/test_listening_reliability.py`, `tests/test_b5_full_control.py`
- `.env.example`, `docs/PROGRESS.md`, `docs/ULTRON_PROJECT_BIBLE.md`

**Changed by the live-GUI fix commit (second session):**
- `assistant/tools/apps.py` — 15-char `comm` alias in `_process_names_for`; capture
  `flatpak ps` stdout/stderr in `_default_flatpak_runner`.
- `tests/test_phase8_eyes_hands_ears.py` — 3 tests for those fixes.
- `docs/PROGRESS.md`, `docs/ULTRON_PROJECT_BIBLE.md`, `left-work-phase8/README.md`,
  `left-work-phase8/SESSION_STATE.md` — live evidence and resume state.

**Not committed (by design):** `.env`, `models/`, `workspace/`, `logs/`, `.backups/`,
`.venv/`.

## Key implementation notes (for a fast catch-up)

- `assistant/vision/atspi_helper.py` is stdlib+gi only, runs under `/usr/bin/python3`, and
  speaks JSON lines: `snapshot`, `click`, `focus`, `set_text`. It filters `gnome-shell`
  and helpers, caps nodes/depth, uses WINDOW+origin for GTK4 bounds, and re-numbers refs.
- `screen_watcher.snapshot_screen()` order: native AT-SPI → agent-desktop (only if AT-SPI
  failed) → OCR (only when allowed). The watcher passes `allow_ocr=False`; the tools allow
  it on demand.
- `click_element` order: AT-SPI action → `ydotool mousemove --absolute` + `click 0xC0` →
  browser find. OCR boxes are tried when AT-SPI has no match. `_verify_click` only claims
  `verified=True` when the element disappears.
- `read_screen` reads `SESSION.screen_elements` (watcher) and returns a one/two-sentence
  spoken reply; `focus_and_type` pastes without moving window focus.
- Fast-path phrases and the static prompt rules are in `fast_path.py` / `prompts.py`.
- `apps.py` launch order: `gio launch <path>` → `gtk-launch <id>` (only if the desktop file
  exists) → `flatpak run <id>` → `Exec`; Chromium browsers prefer their executable +
  `--force-renderer-accessibility`.
- All GUI/Flatpak/Electron launches keep all-DEVNULL + `start_new_session=True` +
  `close_fds=True` (Phase 7 EPIPE rule).
- `_process_names_for()` returns both the full executable name and its 15-character
  `comm` alias; `pgrep -x` needs the alias for `gnome-calculator`, `gnome-text-editor`,
  `gnome-control-center`, etc.
- `_default_flatpak_runner()` captures stdout/stderr (it feeds `flatpak ps`); only detached
  GUI/Electron launches use DEVNULL.
