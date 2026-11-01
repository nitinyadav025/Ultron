# Ultron progress — Phase 5 Part B1

Last updated: 2026-11-01 (listening reliability fix)

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
