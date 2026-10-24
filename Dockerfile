# The package-check target is an installation check, not the Desktop runtime.
# The runtime target is the trusted Telegram/HTTP relay; a native host is required.
FROM python:3.12-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70 AS package-build
ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
COPY pyproject.toml ./
COPY src/ ./src/

RUN python -m pip wheel --no-deps --wheel-dir /wheels . \
    && python -m pip install --no-deps --no-index --target /opt/package /wheels/*.whl

FROM package-build AS package-check
RUN python -m pip install . \
    && python -c "import codex_telegram; import codex_telegram.telegram; import codex_telegram.server" \
    && codex-telegram --help \
    && codex-telegram-desktop --help

FROM python:3.12-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70 AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/package \
    HOME=/tmp
RUN groupadd --gid 10001 relay \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin relay \
    && mkdir /data && chown 10001:10001 /data
COPY --from=package-build /opt/package /opt/package
WORKDIR /opt/package
USER 10001:10001
EXPOSE 8787 8788
ENTRYPOINT ["python", "-m", "codex_telegram.relay_server"]
CMD ["--help"]
