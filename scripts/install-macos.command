#!/bin/bash
set -euo pipefail
if [ "$(uname -s)" != "Darwin" ]; then
  echo "This installer is for macOS. Use install-linux.sh or install-windows.ps1."
  exit 1
fi
# Finder-launched shells may omit the two normal Homebrew paths.
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
cd -- "$(dirname -- "$0")/.."
PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' 2>/dev/null; then
    PYTHON="$candidate"
    break
  fi
done
if [ -z "$PYTHON" ]; then
  echo "Install Python 3.11+ from python.org, then rerun this installer."
  exit 1
fi
"$PYTHON" -m venv .venv
.venv/bin/python -m pip install .
echo "Package installed. Install the official Codex CLI if needed, and prepare your HTTPS tunnel."
.venv/bin/codex-telegram setup
echo "Next: .venv/bin/codex-telegram login"
echo "Then: .venv/bin/codex-telegram doctor --protocol"
echo "Run: .venv/bin/codex-telegram run"
