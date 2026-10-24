#!/bin/sh
set -eu
cd -- "$(dirname -- "$0")/.."
python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' || { echo 'Python 3.11+ is required.'; exit 1; }
python3 -m venv .venv
.venv/bin/python -m pip install .
echo 'Use a running Secret Service/KWallet session, or provide TELEGRAM_BOT_TOKEN only in this process environment.'
.venv/bin/codex-telegram setup
echo 'Next: .venv/bin/codex-telegram login'
echo 'Then: .venv/bin/codex-telegram doctor --protocol'
echo 'Run: .venv/bin/codex-telegram run'
