# Phase 8 — left-work handoff (start here)

Saved **2026-11-05** because the user had to leave. This folder is the exact catch-up
point for the next session.

## Current state in one line

**All Phase 8 code, tests and docs are written and committed locally on
`phase8-eyes-hands-ears` (commit `aad7d29`). 537 tests pass. The only work left is Nitin's
live microphone / GUI / app-launch verification** — the coding session cannot press real
keys, click real windows or use audio hardware.

## Read in this order

1. `ORIGINAL_PROMPT.md` — the user's full Phase 8 request, verbatim.
2. `SESSION_STATE.md` — exact state, evidence, caveats, remaining checklist, resume commands.
3. `../docs/ULTRON_PROJECT_BIBLE.md` — architecture and long-term history.
4. `../docs/PROGRESS.md` — chronological log; the Phase 8 section at the top is current.

## What is left (short)

- [ ] Live voice/GUI test list in `SESSION_STATE.md` (items 1–8).
- [ ] Optional: `sudo dnf install -y tesseract` for the live OCR layer (needs Nitin).
- [ ] Decide `small.en` vs `base.en` after hearing the measured 1.75–2.08 s latency.
- [ ] Verify the browser `Ctrl+F → Esc → Enter` click tier live.
- [ ] Verify `gio launch` opens/raises terminal, settings, files, calculator, text editor,
      Lutris, YouTube.
- [ ] After the live pass, update `docs/PROGRESS.md` (mark UNVERIFIED items verified) and
      make one final local commit. Do not push.

## Do not do

- Do not `git push` or touch `origin`.
- Do not stage `.env`, `models/`, `workspace/`, `logs/`, `.backups/` or `.venv/`.
- Do not run live tests that press keys or click windows from pytest.
- Do not guess a live result; mark it UNVERIFIED until Nitin reports it.
