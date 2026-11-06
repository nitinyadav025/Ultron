# Ultron Browser Bridge

100% local extension that lets Ultron see and control Brave/Firefox tabs. It talks only to
`http://127.0.0.1:9932` (the Ultron environment server).

## Brave / Chrome / Chromium

1. Start Ultron first: `.venv/bin/python main.py environment`
2. Open `brave://extensions` (or `chrome://extensions`).
3. Enable **Developer mode**.
4. Click **Load unpacked** and select this folder (`extensions/ultron-browser-bridge/`).
5. Open/refresh WhatsApp, Gmail, YouTube, Gemini or Claude tabs.

## Firefox

1. Start Ultron first: `.venv/bin/python main.py environment`
2. Open `about:debugging#/runtime/this-firefox`.
3. Click **Load Temporary Add-on…** and select `manifest.firefox.json`.

Temporary add-ons are removed when Firefox closes; repeat after a restart.

## What it sends (localhost only)

Every 800 ms: open tab metadata (`tabId`, `windowId`, `title`, `url`, `active`, `audible`,
`browser`) and the active tab's visible interactive elements (`@b1…@bN`) plus page-specific
extracts (WhatsApp unread/chat/messages, Gemini/Claude/ChatGPT prompt and latest answer,
YouTube search/videos, Gmail unread rows). No cookies, passwords or keystrokes are sent
anywhere except the local Ultron server.

Surface commands it executes: `switch_tab`, `close_tab`, `open_url`, `click_dom`,
`type_dom`, `read_dom`, `scroll_dom`.
