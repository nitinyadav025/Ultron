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
