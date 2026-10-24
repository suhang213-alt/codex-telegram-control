# Packaging check only. Do not present this as a Desktop bridge runtime image.
# Existing Desktop chats require a native host connector on macOS/Windows.
FROM python:3.12-slim-bookworm AS package-check

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
COPY pyproject.toml ./
COPY src/ ./src/

RUN python -m pip install . \
    && python -c "import codex_telegram; import codex_telegram.telegram; import codex_telegram.server" \
    && codex-telegram --help \
    && codex-telegram-desktop --help
