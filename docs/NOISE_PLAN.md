# Noise plan — the room fan

Ultron should ignore steady background noise (the room fan) and react only to the user's
voice. This file records what is installed on this machine, what the user can run, and
what is still UNVERIFIED.

**Do not point the fan at the laptop microphone.** That one change helps more than any
filter.

## What was checked on 2026-11-01 (read-only, no sudo)

| Command | Result |
|---|---|
| `pw-record --help \| grep -i target` | `--target  Set node target serial or name (default auto)` — **supported** |
| `wpctl status` (sources only) | one audio source: `Built-in Audio Analog Stereo` `[vol: 0.21]`; no filter source yet |
| `wpctl inspect @DEFAULT_AUDIO_SOURCE@` | `node.name = "alsa_input.pci-0000_00_1f.3.analog-stereo"`, `object.serial = "50"` |
| `rpm -q ladspa-noise-suppression-for-voice` | **not installed** |
| `ls /usr/lib/ladspa /usr/lib64/ladspa` | **both directories do not exist** |

Because `pw-record` supports `--target`, Ultron now has a setting **`MIC_SOURCE_NAME`**
(blank by default). When set, every `pw-record` call gets `--target <name>`. Blank means
"default source", exactly as before.

## Option A — PipeWire filter-chain with RNNoise (recommended first try)

All steps below are for **the user**, with sudo. **Every command is UNVERIFIED**: the
coding tool did not run anything with sudo, so package availability and the config file
syntax must be checked on this Fedora release.

1. Install the RNNoise LADSPA plugin (user runs this, not the coding tool):

   ```sh
   sudo dnf install ladspa-noise-suppression-for-voice
   ```

   Then check it exists:

   ```sh
   ls /usr/lib64/ladspa/librnnoise_ladspa.so /usr/lib/ladspa/librnnoise_ladspa.so
   ```

   One of the two paths should print. `rpm -q ladspa-noise-suppression-for-voice` should
   stop saying "not installed".

2. Create a user config file (no sudo needed):

   ```sh
   mkdir -p ~/.config/pipewire/pipewire.conf.d
   nano ~/.config/pipewire/pipewire.conf.d/99-noise-cancel.conf
   ```

   Content (UNVERIFIED; the plugin path must match step 1):

   ```conf
   context.modules = [
     { name = libpipewire-module-filter-chain
       args = {
         node.description = "Noise Canceling source"
         media.name       = "Noise Canceling source"
         filter.graph = {
           nodes = [
             {
               type   = ladspa
               name   = rnnoise
               plugin = /usr/lib64/ladspa/librnnoise_ladspa.so
               label  = noise_suppressor_mono
               control = { "VAD Threshold (%)" = 50.0 "VAD Grace Period (ms)" = 200 "Retroactive VAD Grace (ms)" = 0 }
             }
           ]
         }
         capture.props = {
           node.name      = "effect_input.rnnoise"
           media.class    = Audio/Source
         }
         playback.props = {
           node.name      = "effect_output.rnnoise"
           node.passive   = true
         }
       }
     }
   ]
   ```

3. Restart the user audio services (UNVERIFIED; sound stops for a second):

   ```sh
   systemctl --user restart pipewire pipewire-pulse wireplumber
   ```

4. Check that the new source exists:

   ```sh
   wpctl status
   ```

   Look under **Sources** for `Noise Canceling source`. Get its node name:

   ```sh
   wpctl inspect @DEFAULT_AUDIO_SOURCE@ | grep node.name
   ```

   (first select it as default, or inspect the name printed in `wpctl status`).

5. Point Ultron at it in `.env`:

   ```env
   MIC_SOURCE_NAME=effect_input.rnnoise
   ```

   Use the exact `node.name` from step 4. `pw-record --target` accepts a node name.
   Then test with `voice-test` (the user runs it; the coding tool cannot use the mic).

If the recording is garbled or silent, remove the file, restart the three services again,
and clear `MIC_SOURCE_NAME`.

## Option B — EasyEffects (fallback)

1. `sudo dnf install easyeffects` (UNVERIFIED package name).
2. Open EasyEffects → **Input** tab → add **Noise Reduction** (RNNoise) → enable it.
3. In GNOME Settings → Sound, choose EasyEffects' virtual input as the microphone.
4. Leave `MIC_SOURCE_NAME` blank; the default source is already filtered.

## Known risks

- A PipeWire update can rename nodes or change the filter-chain syntax, leaving silence
  or garbled audio. If that happens, remove the config file and restart PipeWire.
- Filter chains add a few milliseconds of latency; Ultron's own 10 dB SNR gate is the
  second line of defence and keeps working without any filter.
- None of the above has been run or verified by the coding tool. Only the two read-only
  findings in the table above are facts.
