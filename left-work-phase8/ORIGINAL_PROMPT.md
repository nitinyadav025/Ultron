# Original Phase 8 prompt (verbatim)

> Saved 2026-11-05 for the next session. This is the user's message exactly as given,
> including typos. Do not treat the paths in it as verified: the real working folder is
> `/home/nitinyadav/Downloads/ULTRON` (see `SESSION_STATE.md`).

---

Read the saved project file for the full clarity about this projecct. You are upgrading Ultron in ~/Downloads/assistant-project on Fedora 44 GNOME Wayland (Python 3.14 in .venv, GTX 1650 4GB VRAM) to Phase 8: Real-Time Screen Eyes & Hands (Vercel agent-desktop + AT-SPI2 + OCR + ydotool), Faster-Whisper `small.en` Screen-Primed Listening, and Universal `gio launch` App Control.

HONESTY RULES (MANDATORY):
1. Never guess when code or logs prove the cause. Read every target file before editing.
2. Never claim a live microphone, speaker, or live GUI result passes without hardware; mark live hardware checks as UNVERIFIED until Nitin runs them.
3. Never touch real logs/assistant.log, logs/audit.jsonl, or workspace/ during pytest (conftest.py must isolate all paths).
4. Always stop any background llama-server, ydotoold, or ScreenWatcher threads before finishing.
5. Never run `git push` or touch `origin`. Stage files by exact filename only (never `git add .` or `git add -A`).

CREATE BRANCH FIRST:
Create and switch to a new branch from current HEAD (`phase7-llm-brain-wayland`):
`git checkout -b phase8-eyes-hands-ears`

IMPLEMENT ALL 6 PARTS BELOW COMPLETELY AND VERIFY WITH PYTEST:

PART 1 — INSTALL & ENABLE VERCEL `agent-desktop` + GNOME WAYLAND AT-SPI2 ACCESSIBILITY BUS
1. Check and install `agent-desktop` CLI (if `npm` or `npx` is available on the system, try `npm install -g agent-desktop --allow-scripts=agent-desktop` or prefix `~/.local` so `~/.local/bin/agent-desktop` is available without sudo; if offline or blocked, log clearly and rely on our native Python AT-SPI2 D-Bus + OCR engine which implements the exact same `@ref` snapshot/act protocol).
2. Enable GNOME Accessibility & Introspection immediately and at startup in `assistant/brain/context.py`:
   - `gsettings set org.gnome.desktop.interface toolkit-accessibility true`
   - `gsettings set org.gnome.shell introspect true`
   - Set `os.environ["ACCESSIBILITY_ENABLED"] = "1"` and `os.environ["QT_ACCESSIBILITY"] = "1"` in `assistant/config.py` / `main.py` so every child app (GTK, Qt, Electron, Firefox, Brave) registers on the `org.a11y.Bus` AT-SPI2 accessibility bus!
   - When launching Chromium/Brave/Chrome in `apps.py` or `web.py`, pass `--force-renderer-accessibility` so web page buttons, links, search bars, and YouTube controls (like `"Show more"`, `"Skip Ad"`, video titles, inputs) appear directly in the accessibility tree!

PART 2 — GIVE ULTRON REAL-TIME "EYES": CONTINUOUS SCREEN WATCHER (`assistant/vision/screen_watcher.py` & `assistant/brain/context.py`)
Build `assistant/vision/screen_watcher.py` implementing the Vercel `agent-desktop` `@ref` snapshot architecture + AT-SPI2 + OCR fallback:
1. **3-Layer UI Element Discovery (`snapshot_screen()`):**
   - **Layer A — `agent-desktop` CLI adapter (if binary installed):** Can invoke `agent-desktop snapshot --skeleton -i --compact` or `agent-desktop list-windows`.
   - **Layer B — Native Python AT-SPI2 / D-Bus & GI Tree Walker (Primary zero-dependency Linux engine):**
     Use Python (`gi.repository.Atspi` if available in system/venv, or `gdbus` / `python3 -c "import gi..."` via `/usr/bin/python3` since Fedora 44 ships `python3-gobject` and `at-spi2-core` in `/usr/bin/python3`!) to walk the active window and visible application trees on the AT-SPI bus (`org.a11y.Bus`).
     *CRITICAL FEDORA NOTE:* `.venv` may not have system `gi` linked, whereas `/usr/bin/python3` on Fedora 44 has `gi.repository.Atspi` and `dbus` preinstalled! Test `/usr/bin/python3 -c "import gi; gi.require_version('Atspi', '2.0'); from gi.repository import Atspi; print('Atspi OK')"` right now. If `/usr/bin/python3` has `Atspi` (or if `pyatspi`/`gdbus` is available), run a persistent lightweight helper subprocess under `/usr/bin/python3` (`assistant/vision/atspi_helper.py`) that communicates over stdin/stdout JSON in `< 30 ms`!
     For every visible interactive element (`push button`, `toggle button`, `link`, `entry`, `text`, `password text`, `menu item`, `page tab`, `check box`, `combo box`, `heading`, `document web`), extract:
       `{"ref": "@e1", "name": label_or_text, "role": role, "app": app_name, "x": cx, "y": cy, "w": w, "h": h, "focused": bool, "editable": bool}`
   - **Layer C — Visual Screenshot + OCR Fallback (`ocr_screen_elements()`):**
     When AT-SPI has no elements for a window (e.g., canvas/video/unaccessible app) or when `find_element_on_screen(target)` doesn't match any AT-SPI element:
     Capture a fast screenshot (`gnome-screenshot -f /tmp/ultron_screen.png` or GNOME Shell D-Bus `org.gnome.Shell.Screenshot.Screenshot`) and if `tesseract` is installed (`shutil.which("tesseract")`), run `tesseract /tmp/ultron_screen.png stdout --psm 11 tsv` to extract every visible word/phrase on screen with its exact pixel bounding box `(left, top, width, height)` and center `(cx, cy)`!
2. **Continuous Background `ScreenWatcher` Daemon Thread:**
   - Runs as a daemon thread started in `listen` and `chat` modes, refreshing `SESSION.screen_elements` (up to 40 top interactive `@ref` elements in the focused/active window), `SESSION.open_windows`, `SESSION.focused_window`, `SESSION.focused_app`, and `SESSION.active_site` every `1.2 seconds`.
   - Formats a compact summary of visible clickable/editable elements into the `[Context: ... | Visible UI: @e1:button:"Show more", @e2:entry:"Search", ...]` header attached to the current user message so the LLM always sees what is on screen right now!

PART 3 — GIVE ULTRON REAL-TIME "HANDS": SEMANTIC + PHYSICAL UI INTERACTION (`assistant/tools/screen_actions.py`, `assistant/tools/desktop_control.py`, `assistant/brain/fast_path.py`)
Create `assistant/tools/screen_actions.py` and register these new tools in `registry.py`, `agent_loop.py`, `fast_path.py`, and `tool_replies.py`:
1. `click_element(target: str)`:
   - Finds the best matching element on screen by `@ref` (e.g. `@e5`), exact/fuzzy label match (e.g. `"Show more"`, `"Expand"`, `"Skip Ad"`, `"Subscribe"`, `"Play"`, `"Search"`, `"Save"`, `"Submit"`, `"First video"`), or role.
   - **Observe → Act → Verify Loop:**
     a) First attempts semantic AT-SPI action (`do_action(0)` / `click` via `atspi_helper.py` or `agent-desktop`).
     b) If semantic click is unsupported or fails (or on Wayland web elements), uses the element's bounding box center `(x, y)` (or OCR bounding box center) to move the pointer and click via `ydotool`:
        `ydotool mousemove --absolute -x <x> -y <y>` followed by `ydotool click 0xC0` (left click).
     c) If the target is `"show more"` / `"expand"` on YouTube/web and coordinates are unavailable, sends `/` or `Tab` navigation or searches text on page (`Ctrl+F` -> `<target>` -> `Esc` -> `Enter`)!
       *WAIT — Look at that `Ctrl+F` → `<target>` → `Esc` → `Enter` trick for browsers!*
       In Firefox, Brave, and Chrome on Wayland, if a user says `"Click on <any button or link text>"` or `"Expand the show more section"` and AT-SPI coordinates aren't available: pressing `Ctrl+F`, typing the link/button text (`"Show more"`), pressing `Esc` (which leaves keyboard focus highlighted right on the matched link/button in Firefox/Chromium!), and pressing `Enter` activates any link or button on any webpage 100% reliably on Wayland!
       Implement **3-tier click execution** in `click_element(target)`:
       - Tier 1: AT-SPI semantic `do_action` (`atspi_helper.py` / `agent-desktop`).
       - Tier 2: AT-SPI or OCR pixel coordinates `(cx, cy)` -> `ydotool mousemove` + `ydotool click`.
       - Tier 3 (Browser In-Page Smart Click): When in Firefox/Brave/Chrome and Tiers 1–2 don't find coordinates, use `Ctrl+F` -> paste `target` -> `Esc` -> `Enter`!
2. `read_screen(query: str = "")`:
   - Reads out loud what is currently visible on the screen: active window title, current website/app, headings, and top interactive buttons/links/text (or answers a specific `query` about what is on the screen right now).
   - Map natural commands like `"what is on my screen"`, `"read the screen"`, `"what windows are open"`, `"what can I click"`, `"expand the show more section"`, `"click on <X>"`, `"press the <X> button"`, `"select <X>"` in both `fast_path.py` and the LLM system prompt!
3. `focus_and_type(target_field: str, text: str)`:
   - Finds the input field matching `target_field` (e.g. `"email"`, `"search"`, `"username"`, `"message"`, or the focused editable element), focuses/clicks it, and pastes `text` via `wl-copy` + `Ctrl+V`.

PART 4 — FIX LISTENING ACCURACY: SWITCH TO `faster-whisper-small.en` + SCREEN-AWARE VOCABULARY PRIMING (`assistant/speech/ears.py`, `assistant/config.py`, `.env`, `assistant/speech/wake.py`)
1. Check `models/faster-whisper-small.en`:
   - Verify `models/faster-whisper-small.en` exists locally.
   - Switch default `WHISPER_MODEL` in `assistant/config.py` and `.env` from `base.en` to `small.en` (keeping automatic fallback to `base.en` if `small.en` directory is missing in unit test mocks).
2. Optimize `small.en` speed on CPU in `assistant/speech/ears.py` so latency stays fast (`~0.55–0.75s`):
   - Initialize `WhisperModel` with `device="cpu"`, `compute_type="int8"`, `cpu_threads=6`, `num_workers=2`.
   - In `transcribe()`: pass `beam_size=2`, `best_of=2`, `temperature=0.0`, `condition_on_previous_text=False`, and `vad_filter=True` with `vad_parameters={"min_silence_duration_ms": 250, "speech_pad_ms": 200}` so Whisper strips leading/trailing silence before running the transformer encoder!
3. Dynamic Screen-Aware Hotwords & Prompt (`assistant/speech/ears.py`):
   - Build `build_dynamic_hotwords()` which combines core wake/command terms (`Ultron, Firefox, Brave, GitHub, YouTube, Discord, Lutris, Text Editor, Screenshot`) with the names of currently open apps and up to 8 visible short button/tab labels from `SESSION.screen_elements`!
   - Expand phonetic command repair in `assistant/speech/wake.py` to fix common Indian-English / Whisper near-misses (`"show moor"` -> `"show more"`, `"expand the show more"` -> `"click show more"`, `"loot risk"` / `"loo tris"` / `"hermes"` -> `"lutris"`, `"screen shot"` / `"take a screen shot"` -> `"take screenshot"`, `"right"` -> `"write"`, `"dis cord"` -> `"discord"`).

PART 5 — UNIVERSAL APP LAUNCHER & WINDOW ACTIVATION (`assistant/tools/apps.py`)
1. Why Ultron couldn't open all apps: manual `Exec=` string splitting breaks on `DBusActivatable=true`, Flatpak wrappers, and GNOME system utilities.
2. In `assistant/tools/apps.py`:
   - Store the full `.desktop` file path (`desktop_path`) and desktop ID (`desktop_id`, e.g., `org.gnome.TextEditor.desktop`, `com.discordapp.Discord.desktop`, `net.lutris.Lutris.desktop`) for every indexed application across `/usr/share/applications`, `/var/lib/flatpak/exports/share/applications`, `~/.local/share/applications`, and `/var/lib/snapd/desktop/applications`.
   - When `open_app(app_name)` launches a `.desktop` entry:
     a) Prefer `gio launch <desktop_path>` (with `stdin/stdout/stderr=DEVNULL, start_new_session=True, close_fds=True`). `gio launch` is GNOME's native GLib launcher that handles Flatpaks, DBusActivatable single-instance apps, and automatically raises/focuses already-running Wayland windows!
     b) If `gio` is unavailable, try `gtk-launch <desktop_id>`, then `flatpak run <app_id>`, then cleaned `Exec=` binary.
   - Add a comprehensive natural alias table in `apps.py` mapping everyday spoken names to Fedora 44 GNOME apps:
     * `"terminal"`, `"console"`, `"shell"`, `"command prompt"` -> `ptyxis` / `org.gnome.Ptyxis.desktop` / `gnome-terminal`
     * `"settings"`, `"system settings"`, `"control center"`, `"wifi"`, `"bluetooth"`, `"sound settings"`, `"display settings"` -> `gnome-control-center` / `org.gnome.Settings.desktop`
     * `"files"`, `"file manager"`, `"folder"`, `"nautilus"`, `"downloads"`, `"documents"` -> `nautilus` / `org.gnome.Nautilus.desktop`
     * `"text editor"`, `"notepad"`, `"editor"`, `"document editor"` -> `gnome-text-editor` / `org.gnome.TextEditor.desktop`
     * `"calculator"`, `"calc"` -> `gnome-calculator` / `org.gnome.Calculator.desktop`
     * `"software"`, `"app store"`, `"store"` -> `gnome-software` / `org.gnome.Software.desktop`
     * `"system monitor"`, `"task manager"`, `"processes"` -> `gnome-system-monitor`
     * `"lutris"`, `"loot risk"`, `"games"` -> `lutris` / `net.lutris.Lutris.desktop`
     * `"tweaks"`, `"gnome tweaks"` -> `gnome-tweaks`
     * `"extensions"` -> `gnome-extensions-app`
     * Plus all web-app fallbacks (`youtube`, `github`, `discord`, `whatsapp`, `chatgpt`, `claude`, `gmail`, `spotify`, `netflix`, `reddit`, `instagram`, `google`).
   - Also upgrade `switch_app(app_name)` and `focus_target_window()` on GNOME Wayland: if `wmctrl -a` fails (because the app is a native Wayland window), run `gio launch <desktop_path>` or `gtk-launch <desktop_id>` so GNOME Wayland activates and raises the existing window to the foreground!

PART 6 — AUTOMATED TESTS, BENCHMARK & DOCUMENTATION (`tests/test_phase8_eyes_hands_ears.py`)
1. Write comprehensive unit and integration tests in `tests/test_phase8_eyes_hands_ears.py` testing:
   - `ScreenWatcher` & `snapshot_screen()` parsing AT-SPI / `agent-desktop` / OCR elements into `@ref` items (`@e1`, `@e2`) and injecting `[Visible UI: ...]` into the LLM user turn while keeping the `system` prompt 100% static for KV-cache slot 0 reuse.
   - `click_element("Show more")` and `"Expand the show more section"` executing Tier 1 (AT-SPI action), Tier 2 (`ydotool mousemove` + `click` at bounding box center), and Tier 3 (Browser `Ctrl+F` -> text -> `Esc` -> `Enter`).
   - `read_screen()` summarizing the active window, site, and visible interactive buttons/inputs.
   - `focus_and_type("email", ...)` focusing an input field and pasting via `wl-copy` + `Ctrl+V`.
   - `ears.py` defaulting to `small.en` (when present) with `beam_size=2`, `vad_filter=True`, and dynamic screen-aware hotwords.
   - `open_app` and `switch_app` using `gio launch <desktop_path>` with `DEVNULL` streams and resolving all Fedora 44 natural aliases (`terminal`, `settings`, `files`, `calculator`, `text editor`, `lutris`).
2. Run `.venv/bin/python -m pytest -q` and `.venv/bin/python main.py benchmark`.
3. Update `docs/PROGRESS.md` and `docs/ULTRON_PROJECT_BIBLE.md`, commit cleanly on `phase8-eyes-hands-ears`, and stop all background processes (`llama-server`, `ydotoold`, helper processes).
