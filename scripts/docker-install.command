#!/bin/sh
set -eu
if [ "$(uname -s)" != Darwin ]; then
  printf '%s\n' 'Use this installer on the Mac with the existing Desktop app.' >&2
  exit 2
fi
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPOSITORY_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
command -v python3 >/dev/null 2>&1 || {
  printf '%s\n' 'Install Python 3.11+ from python.org, then retry.' >&2
  exit 2
}
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 2)'
python3 -m venv "$REPOSITORY_DIR/.venv"
"$REPOSITORY_DIR/.venv/bin/python" -m pip install "$REPOSITORY_DIR"
"$REPOSITORY_DIR/.venv/bin/python" "$SCRIPT_DIR/docker.py" desktop-setup
"$REPOSITORY_DIR/.venv/bin/python" "$SCRIPT_DIR/docker.py" setup
printf '%s\n' 'Setup complete. Follow docs/DOCKER.md to pull, check and start the container and native host.'
