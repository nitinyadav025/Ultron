# Phase 8 — left-work handoff (start here)

Saved **2026-11-05** because the user had to leave. This folder is the exact catch-up
point for the next session.

## Current state in one line

**All Phase 8 code, tests and docs are written and committed locally on
`phase8-eyes-hands-ears` (base commit `aad7d29`, plus the live-GUI fix commit). 540 tests
pass. Live `gio launch` + AT-SPI + `close_app` is verified for calculator, text editor,
files, terminal and settings; Lutris starts as a Flatpak but shows no window. The remaining
work is Nitin's live microphone / voice / click / typing verification** — the coding session
cannot press real keys, click real windows or use audio hardware.

## Read in this order

1. `ORIGINAL_PROMPT.md` — the user's full Phase 8 request, verbatim.
2. `SESSION_STATE.md` — exact state, evidence, caveats, remaining checklist, resume commands.
3. `../docs/ULTRON_PROJECT_BIBLE.md` — architecture and long-term history.
4. `../docs/PROGRESS.md` — chronological log; the Phase 8 section at the top is current.

## What is left (short)

- [ ] Live voice test list in `SESSION_STATE.md` (items 1–5, 7–8; item 6 launcher verified).
- [ ] Optional: `sudo dnf install -y tesseract` for the live OCR layer (needs Nitin).
- [ ] Decide `small.en` vs `base.en` after hearing the measured 1.75–2.08 s latency.
- [ ] Verify the browser `Ctrl+F → Esc → Enter` click tier live.
- [ ] Verify `gio launch` raises YouTube, and investigate why Lutris shows no window.
- [ ] After the live pass, update `docs/PROGRESS.md` (mark the remaining UNVERIFIED items
      verified) and make one final local commit. Do not push.

Two live bugs were found and fixed in the second session (see `SESSION_STATE.md`): the
15-character `pgrep`/`comm` truncation and the discarded `flatpak ps` output. Both have
tests; the suite is **540 passed**.

## Do not do

- Do not `git push` or touch `origin`.
- Do not stage `.env`, `models/`, `workspace/`, `logs/`, `.backups/` or `.venv/`.
- Do not run live tests that press keys or click windows from pytest.
- Do not guess a live result; mark it UNVERIFIED until Nitin reports it.
