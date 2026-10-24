#!/bin/sh
set -eu
if [ "$(uname -s)" != "Darwin" ]; then
  printf '%s\n' 'This installer is for the existing macOS Desktop app.' >&2
  exit 2
fi
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPOSITORY_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
command -v python3 >/dev/null 2>&1 || {
  printf '%s\n' 'Install Python 3.11 or newer from python.org, then run this installer again.' >&2
  exit 2
}
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 2)' || {
  printf '%s\n' 'Python 3.11 or newer is required.' >&2
  exit 2
}
python3 -m venv "$REPOSITORY_DIR/.venv"
"$REPOSITORY_DIR/.venv/bin/python" -m pip install "$REPOSITORY_DIR"
"$REPOSITORY_DIR/.venv/bin/codex-telegram-desktop" setup
printf '%s\n' 'Setup complete. Activate .venv/bin/activate, then close Desktop normally and run codex-telegram-desktop launch.'
