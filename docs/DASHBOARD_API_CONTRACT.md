# Ultron Environment — Dashboard API Contract (Gate 2B)

> For the Apple visionOS Liquid Glass V18 dashboard agent. The backend is finished and
> tested (Gate 2A). Build the frontend only; do not change Python behaviour without asking.
>
> Base URL: `http://127.0.0.1:9932` (override with `ULTRON_ENV_PORT`).
> CORS: every response has `Access-Control-Allow-Origin: *`.
> JSON errors: `{"ok": false, "error": "..."}` with an HTTP 4xx/5xx status.

## 1. Where the frontend lives

Put the production build in **`assistant/dashboard/dist/`** (fallback directory:
`assistant/dashboard/static/`). `assistant/environment/server.py` serves:

- `GET /` → `dist/index.html`
- `GET /<asset>` → the matching file inside `dist/` (path traversal is rejected)
- If neither directory has `index.html`, a built-in readiness page is served with HTTP 200.

The dashboard must be a static bundle (HTML/CSS/JS). It may not start a second server. Use
relative asset URLs so it works under `http://127.0.0.1:9932/`.

## 2. Start / preview

```sh
cd ~/Downloads/ULTRON
.venv/bin/python main.py environment            # API + voice + Dashboard window
.venv/bin/python main.py environment --headless # API + voice only
.venv/bin/python main.py environment --port 9932
```

Open `http://127.0.0.1:9932/`. The same command starts llama-server, ydotool, the screen
watcher, the voice controller and the browser bridge endpoint. Stop with `Ctrl+C`
(SIGINT/SIGTERM) — every child process is closed.

## 3. `GET /api/health`

```json
{
  "ok": true,
  "version": "phase9-env",
  "llama_server": true,
  "voice_status": "listening"
}
```

`voice_status` is the same value as `status` in `/api/state`.

## 4. `GET /api/state` — full live snapshot

```json
{
  "revision": 128,
  "ts": "2026-11-06T23:20:00+05:45",
  "started_at": "2026-11-06T23:18:00+05:45",
  "status": "listening",
  "voice_mode": "wake_word",
  "mic_muted": false,
  "tts_muted": false,
  "audio_level_rms": 812,
  "audio_peak": 9210,
  "current_transcript": "check what messages came in on whatsapp",
  "current_thought": "Thinking about: check whatsapp",
  "current_action": {
    "tool": "read_screen",
    "args": {"query": "WhatsApp"},
    "status": "done",
    "timestamp": "2026-11-06T23:19:58+05:45"
  },
  "next_planned_steps": [],
  "last_reply": "You have two unread chats: Nitin and the Ultron group.",
  "conversation_feed": [
    {
      "id": 17,
      "timestamp": "2026-11-06T23:19:58+05:45",
      "role": "user",
      "text": "check what messages came in on whatsapp",
      "tools_called": [],
      "latency_ms": 0,
      "source": "voice"
    },
    {
      "id": 18,
      "timestamp": "2026-11-06T23:19:59+05:45",
      "role": "assistant",
      "text": "You have two unread chats: Nitin and the Ultron group.",
      "tools_called": ["interact_with_tab"],
      "latency_ms": 2210.4,
      "source": "voice"
    }
  ],
  "screen_state": {
    "focused_window": "WhatsApp - Brave",
    "focused_app": "brave",
    "active_site": "whatsapp",
    "open_windows": ["WhatsApp - Brave", "Gemini - Brave"],
    "visible_elements": [
      {"ref": "@e1", "name": "Chats", "role": "tab", "x": 12, "y": 48, "w": 80, "h": 32}
    ],
    "browser_tabs": [
      {"browser": "brave", "tabId": 11, "windowId": 1, "title": "WhatsApp",
       "url": "https://web.whatsapp.com/", "active": true, "audible": false}
    ]
  },
  "browser_tabs": [
    {"browser": "brave", "tabId": 11, "windowId": 1, "title": "WhatsApp",
     "url": "https://web.whatsapp.com/", "active": true, "audible": false,
     "pinned": false}
  ],
  "telemetry": {
    "gpu_vram_used_mb": 1612,
    "gpu_vram_total_mb": 4096,
    "gpu_util_pct": 16.0,
    "cpu_pct": 11.0,
    "ram_used_mb": 9067,
    "llama_server_alive": true,
    "p50_ms": 2210.4,
    "p95_ms": 3050.2,
    "latency_series": [
      {"ts": "2026-11-06T23:19:59+05:45", "transcribe_ms": 1800.0,
       "llm_ms": 410.4, "tool_ms": 0.0, "total_ms": 2210.4}
    ]
  },
  "live_config": {
    "assistant_name": "Ultron",
    "user_name": "Nitin",
    "personality_prompt": "",
    "reply_style": "natural",
    "brain_replies": true,
    "voice_silence_ms": 2600,
    "voice_max_seconds": 60.0,
    "listen_early_check_ms": 1800,
    "listen_followup_s": 8.0,
    "mic_source_volume": 0.21,
    "search_engine": "google",
    "permission_mode": "auto-safe"
  }
}
```

Field notes:

- `status`: `starting | online | listening | transcribing | thinking | speaking | muted | error`.
- `voice_mode`: `wake_word | continuous | ptt`.
- `conversation_feed`: ring buffer, last **50** turns, newest last.
- `latency_series`: ring buffer, last **30** turns; `p50_ms`/`p95_ms` are computed over it.
- `screen_state.visible_elements`: AT-SPI `@e1…@eN` elements when available; fields may
  include `name`, `role`, `app`, `x/y/w/h`, `focused`, `editable`, `actions`.
- `browser_tabs`: extension/CDP/window-sensor tabs; `source` is added for `cdp`/`wmctrl`.
- `revision` is monotonic; use it to skip duplicate renders.

## 5. `GET /api/stream` — Server-Sent Events

Headers: `Content-Type: text/event-stream; charset=utf-8`, `Cache-Control: no-store`.
Immediately sends `retry: 1000`, then a full snapshot on every state change and at least
every 250 ms:

```text
retry: 1000

id: 128
event: state
data: { ...same object as GET /api/state... }

id: 129
event: state
data: { ... }
```

Use the native `EventSource`:

```js
const source = new EventSource("http://127.0.0.1:9932/api/stream");
source.addEventListener("state", (event) => {
  const state = JSON.parse(event.data);
  render(state);
});
```

No custom event types beyond `state`; the connection stays open until the client closes it
or the server stops.

## 6. `POST /api/command`

```json
{"text": "open a new chat in Gemini", "speak": true}
```

- `text` (required): a typed command or conversational paragraph.
- `speak` (optional, default `true`): speak the reply through Piper.

Response (HTTP 200):

```json
{
  "ok": true,
  "reply": "I've opened a new chat in Gemini.",
  "tools_called": ["interact_with_tab"],
  "latency_ms": 1840.2
}
```

While it runs, `/api/state` and `/api/stream` receive `current_transcript`,
`current_thought` (`Thinking about…`, `Running <tool>…`, `Finished <tool>.`),
`current_action` (`running` → `done`), and a new `conversation_feed` pair.

Errors: `400` for an empty text, `500` for a brain failure, `503` if no brain is attached.

## 7. `POST /api/voice/control`

```json
{"action": "start", "mode": "continuous"}
```

`action`: `start | stop | mute_mic | unmute_mic | mute_tts | unmute_tts | barge_in |
ptt_start | ptt_stop`. `mode` is optional and may accompany any action:
`wake_word | continuous | ptt`.

Response:

```json
{
  "ok": true,
  "action": "barge_in",
  "result": true,
  "voice_mode": "continuous",
  "status": "listening"
}
```

- `barge_in` stops Piper playback immediately (<20 ms; it terminates the player process).
- `ptt_start` begins buffering; `ptt_stop` transcribes the buffer immediately with no
  silence timeout.
- `mute_mic`/`mute_tts` reflect in `mic_muted`/`tts_muted` and `status`.

## 8. `GET /api/config` and `POST /api/config`

`GET`:

```json
{"ok": true, "config": { ...live_config... }}
```

`POST` accepts either a flat patch or `{"config": {...}}`:

```json
{"assistant_name": "Jarvis", "personality_prompt": "Warm, witty, concise.", "reply_style": "concise"}
```

Response: `{"ok": true, "config": {...full updated config...}}`.

Every key is validated and clamped; unknown keys are ignored. Valid keys and defaults:

| Key | Type | Default |
|---|---|---|
| `assistant_name` | string | `"Ultron"` |
| `user_name` | string | `"Nitin"` |
| `personality_prompt` | string | `""` |
| `reply_style` | `natural` / `concise` / `detailed` | `"natural"` |
| `brain_replies` | boolean | `true` |
| `voice_silence_ms` | int 300–10000 | `2600` |
| `voice_max_seconds` | float 3–120 | `60.0` |
| `listen_early_check_ms` | int 400–5000 | `1800` |
| `listen_followup_s` | float 0–60 | `8.0` |
| `mic_source_volume` | float 0–1 | `0.21` |
| `search_engine` | string | `"google"` |
| `permission_mode` | `ask` / `auto-safe` / `human-only` | `"auto-safe"` |

Persistence: `workspace/ultron_environment_config.json`; the server also applies the change
to the running voice controller and permission gate immediately.

## 9. `GET /api/apps` and `POST /api/apps/action`

`GET /api/apps`:

```json
{
  "ok": true,
  "apps": [
    {"id": "firefox", "name": "Firefox", "desktop_id": "org.mozilla.firefox.desktop",
     "desktop_path": "/usr/share/applications/org.mozilla.firefox.desktop",
     "category": "system", "is_running": true}
  ],
  "windows": ["WhatsApp - Brave"],
  "focused_window": "WhatsApp - Brave"
}
```

`POST /api/apps/action`:

```json
{"action": "open", "app_name": "calculator"}
```

`action`: `open | close | focus`. The response is the underlying tool result, for example:

```json
{"ok": true, "app_name": "Calculator", "desktop_id": "org.gnome.Calculator.desktop", "reply": "I've opened Calculator."}
```

## 10. `GET /api/tabs` and `POST /api/tabs/action`

`GET /api/tabs`:

```json
{
  "ok": true,
  "count": 3,
  "tabs": [
    {"browser": "brave", "tabId": 11, "windowId": 1, "title": "WhatsApp",
     "url": "https://web.whatsapp.com/", "active": true, "audible": false,
     "pinned": false}
  ],
  "bridge_connected": true
}
```

`POST /api/tabs/action`:

```json
{"action": "click", "tab": "Gemini", "target": "New chat"}
```

`action`: `switch | close | open | click | read | type | send | scroll`.

```json
{"action": "type", "tab": "Claude", "target": "", "text": "Summarize this page", "submit": true}
{"action": "read",  "tab": "WhatsApp"}
{"action": "open",  "url": "https://github.com"}
```

When the extension is connected the action is queued (the response contains `action_id`)
and the next 800 ms poll executes it. `read` returns the cached DOM/extract immediately.
When the extension is absent, the server falls back to wmctrl/AT-SPI/ydotool and the
response contains `"method": "wmctrl" | "atspi"`.

## 11. Browser bridge endpoints (used by the extension)

`POST /api/browser/sync` (the extension pushes every 800 ms):

```json
{
  "browser": "brave",
  "tabs": [
    {"tabId": 11, "windowId": 1, "title": "WhatsApp",
     "url": "https://web.whatsapp.com/", "active": true, "audible": false}
  ],
  "dom": {
    "tabId": 11,
    "url": "https://web.whatsapp.com/",
    "elements": [{"ref": "@b1", "tag": "div", "role": "button", "label": "Chats",
                  "text": "", "placeholder": "", "x": 12, "y": 48}],
    "special": {"host": "web.whatsapp.com", "items": [], "latest": [], "text": "..."}
  },
  "results": [{"id": "abc123", "ok": true}]
}
```

`GET /api/browser/poll?browser=brave` returns `{"ok": true, "actions": [...]}`. The sync
response also includes the same `actions` array. Action ids are single-delivery.

`POST /api/browser/result` accepts `{"id": "...", "ok": true, "error": "..."}`.

## 12. Skills / memory endpoints

`GET /api/skills`:

```json
{
  "ok": true,
  "facts": {"email": "nitin@example.com"},
  "aliases": {"loot risk": "lutris"},
  "skills": [{"trigger": "work mode", "actions": ["open code", "open github"], "source": "user"}]
}
```

`POST /api/skills`:

```json
{"type": "fact",  "name": "email", "value": "nitin@example.com"}
{"type": "alias", "alias": "loot risk", "target": "lutris"}
{"type": "skill", "trigger": "work mode", "actions": ["open code", "open github"]}
```

`DELETE /api/skills` (JSON body or query string):

```json
{"type": "fact", "name": "email"}
{"type": "alias", "alias": "loot risk"}
{"type": "skill", "trigger": "work mode"}
```

All three write through the existing workspace memory/skill files and return the refreshed
`facts`/`aliases`/`skills` object.

## 13. UI implementation notes

- Poll nothing when SSE is connected; `EventSource` reconnects automatically.
- Suggested 60 fps layout: waveform from `audio_level_rms/audio_peak`; status orb from
  `status`; thought ticker from `current_thought`; action chip from `current_action`;
  spline chart from `telemetry.latency_series`; GPU ring from
  `gpu_vram_used_mb / gpu_vram_total_mb`; conversation from `conversation_feed`.
- Voice controls map 1:1 to `/api/voice/control`; the Stop button must send `barge_in`.
- Mode switch sends `{"action":"mode","mode":"wake_word|continuous|ptt"}` (or includes
  `mode` with another action).
- Microphone mute and Piper mute are independent (`mute_mic`, `mute_tts`).
- All requests are localhost-only; no API key or auth header is required.
- Do not add CDNs or remote fonts if offline purity matters; bundle assets in `dist/`.
- Gate 2A tests pin the JSON shapes above; if a change is truly needed, update the backend
  and `docs/LEFT_WORK.md` together.
