# Ultron — Fedora 44 Edition (GNOME 50.5 Wayland | Python 3.14.8)

## Quick Start in VS Code

1. Install system packages:

   ```bash
   sudo dnf install python3-pip python3-devel python3-tkinter git gcc wl-clipboard ydotool gnome-screenshot libnotify pipewire-utils speech-dispatcher
   ```

2. Open `assistant-project` in VS Code (`code assistant-project`), then run in the VS Code terminal:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   cp .env.example .env
   ```

3. Paste your free-tier `GEMINI_API_KEY` into `.env`.

4. Test and run:

   ```bash
   python3 main.py doctor
   python3 main.py self-test
   pytest
   python3 main.py gui   # Launches Ultron Dashboard with Model/Thinking switchers & Voice
   python3 main.py chat  # Launches CLI
   ```
