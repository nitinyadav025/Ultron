# Ultron — Fedora 45 Edition (GNOME 51.1 Wayland | Python 3.14.9)

## Quick Start in VS Code

1. Install system packages:

   ```bash
   sudo dnf install python3-pip python3-devel python3-tkinter git gcc wl-clipboard ydotool gnome-screenshot libnotify pipewire-utils speech-dispatcher
   ```

2. Fedora 45 ships Python 3.15, which has no `onnxruntime`/`av` wheels yet. Install a
   user-space Python 3.14 once, then create the venv with it:

   ```bash
   uv python install 3.14
   ~/.local/bin/python3.14 -m venv .venv
   .venv/bin/python -m pip install -r requirements.txt
   cp .env.example .env
   ```

3. Open `ULTRON` in VS Code (`code ULTRON`), and use `.venv/bin/python` for every command.

4. Test and run:

   ```bash
   .venv/bin/python main.py doctor
   .venv/bin/python main.py self-test
   .venv/bin/python -m pytest
   .venv/bin/python main.py gui   # Launches Ultron Dashboard with Model/Thinking switchers & Voice
   .venv/bin/python main.py chat  # Launches CLI
   ```

`BACKEND=local` needs no API key. `BACKEND=gemini` keeps the cloud rollback working and
reads `GEMINI_API_KEY` from `.env`.
