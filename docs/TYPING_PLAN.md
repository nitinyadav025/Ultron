# Typing plan (detect only — nothing was built)

Goal (later, NOT now): let Ultron type text into an app by voice. This file records what
is installed today and the exact commands **the user** must run. Nothing here is
implemented, and the coding tool ran no sudo.

## What was checked on 2026-11-01 (read-only)

| Command | Result |
|---|---|
| `echo $XDG_SESSION_TYPE` | `wayland` |
| `command -v ydotool dotool wl-copy wl-paste` | `/usr/bin/ydotool`, `/usr/bin/wl-copy`, `/usr/bin/wl-paste` — **dotool is not installed** |
| `rpm -q ydotool` | `ydotool-1.0.4-8.fc44.x86_64` |
| `systemctl is-active ydotool` / `is-enabled ydotool` | `inactive` / `disabled` (no daemon running) |
| `systemctl --user list-unit-files \| grep -i ydotool` | nothing (the unit is a **system** unit) |
| `rpm -ql ydotool` | `/usr/bin/ydotoold`, `/usr/lib/systemd/system/ydotool.service` |
| `/usr/lib/systemd/system/ydotool.service` | `ExecStart=/usr/bin/ydotoold`, `WantedBy=default.target` |
| `ls -l /dev/uinput` | `crw-rw----+ root root` with an ACL `user:nitinyadav:rw-` |
| `id -nG` | `nitinyadav wheel dialout` (not `input`; the ACL is what grants access) |

So: the client `ydotool` and the daemon `ydotoold` are installed, and this user already has
read/write access to `/dev/uinput`. The daemon is simply not running.

## Why wtype and xdotool do not work

On GNOME Wayland, an app cannot inject keystrokes into another window through X11
(`xdotool`) or the old `wtype` path. `ydotool`/`dotool` talk to the kernel `uinput` device
instead, which is why they need the daemon and `/dev/uinput` permission.

## Commands for the user (sudo — UNVERIFIED)

The coding tool did **not** run these. Check each result.

1. Start and enable the daemon:

   ```sh
   sudo systemctl enable --now ydotool.service
   ```

2. Confirm it is running:

   ```sh
   systemctl is-active ydotool
   systemctl status ydotool --no-pager
   ```

   Expected: `active`, and a line starting `/usr/bin/ydotoold`.

3. Find the socket (path depends on the version; UNVERIFIED):

   ```sh
   sudo ss -xl | grep -i ydotool
   ls -l /tmp/.ydotool_socket /run/ydotool.socket 2>/dev/null
   ```

4. Safe manual test **in a scratch editor** (never in a password field):

   ```sh
   ydotool type "ultron typing test"
   ```

   If it prints nothing and types into the focused window, typing works. If it says
   "failed to connect to socket", set `YDOTOOL_SOCKET` to the path found in step 3.

5. `dotool` alternative (only if ydotool fails; **UNVERIFIED that Fedora 44 has it**):

   ```sh
   sudo dnf install dotool
   ```

`wl-copy`/`wl-paste` are installed too, but they only use the clipboard; they cannot type.

## Risks (read before building)

- **Wrong window.** `ydotool` types into whatever window has focus. If focus changes at the
  wrong moment, private text can land in another app.
- **Never auto-press Enter.** Typing a sentence must never submit a form, send a message or
  run a shell command. Only text goes in, and only after an explicit yes.
- **Secrets.** Never type `.env`, API keys, passwords or anything from the clipboard.
- **Clipboard.** Using `wl-copy` overwrites what the user copied. Ask first.
- **No focus detection.** Wayland does not let Ultron reliably read which window is
  focused. The design must therefore ask the user to focus the window, show the exact text
  and the target app, and wait for a spoken/typed "yes".
- **Rate limit.** Cap the text length (for example 200 characters) and do one insertion per
  command.

## Safe design (to build later, if approved)

1. A new `type_text` tool, risk level **confirm**, never run in `listen` (hands-free always
   refuses confirmation-needing tools).
2. Ask before typing: show the exact text and say "I will type this into the focused
   window. Say yes."
3. Type only, never press Enter; stop on Ctrl+C.
4. Refuse `.env`, key-like strings and anything with a newline.
5. Log the request (redacted) in `logs/audit.jsonl` like every other tool.
