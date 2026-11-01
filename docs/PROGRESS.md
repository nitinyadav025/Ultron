# Ultron progress — Phase 5 Part B1

Last updated: 2026-11-01 (follow-up fixes session)

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
| Part 3 | "Say it again" confidence guard + set_volume number guard | NOT STARTED |
| Part 4 | list_files bug: log tool args, never claim "no items" wrongly, robust spoken folder names | NOT STARTED |
| Part 5 | PyAV safety: no file paths to WhisperModel.transcribe, add guard test | NOT STARTED |

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


## Head state

- Local branch for this work: `phase5-b1-work` (from `main` @ 9d95911).
- Task 4 commit: `96287de` "Phase 5 B1 Task 4: finish list_files reply and lead-in guard tests".
- Working tree is clean after `2b7cd20`; nothing was pushed and the remote was not touched.
- To catch up: read this file, then `git switch phase5-b1-work` and run
  `.venv/bin/python -m pytest -q` (expect 114 passed before the follow-up fixes).

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

`114 passed` (0 failed) before the follow-up fixes. Before that session: 110 passed.

## Open problems

- Tasks 1-4 have not been verified with a real microphone/speakers yet.
- No code problems found in Tasks 1-3; nothing was changed there.
- No llama-server is running now (`ss -ltnp | grep 9931` shows no listener).

## NEXT STEP - how to resume

Follow Part 1 in the follow-up table above: run the scratch A/B over the six
`/tmp/cmp2_*.wav` files, then report which stage breaks accuracy. Do not run voice tests
from the coding tool. Do not start Phase 5 Part C without the user's approval.
