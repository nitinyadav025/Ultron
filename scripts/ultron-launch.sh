#!/usr/bin/env bash
# Ultron standalone launcher: starts the environment and opens its Dashboard window.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

if [[ ! -x "$PROJECT_DIR/.venv/bin/python" ]]; then
  echo "Ultron: .venv/bin/python is missing. Rebuild the venv with Python 3.14." >&2
  exit 1
fi

exec "$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/main.py" environment "$@"
