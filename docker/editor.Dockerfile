# syntax=docker/dockerfile:1.7
FROM node:26-bookworm-slim AS ui
WORKDIR /ui
COPY scenario-editor/package*.json ./
RUN --mount=type=cache,target=/root/.npm npm ci --prefer-offline --no-audit --no-fund --fetch-retries=1 --fetch-timeout=20000 --fetch-retry-mintimeout=1000 --fetch-retry-maxtimeout=3000
COPY scenario-editor/index.html scenario-editor/vite.config.js ./
COPY scenario-editor/src ./src
RUN npm run build

# Optional compiler. Cargo dependencies depend only on the lock/manifest, not scenario or UI edits.
FROM rust:1.98-slim-bookworm AS compiler
RUN apt-get update && apt-get install -y --no-install-recommends python3 ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /workspace
COPY Cargo.toml Cargo.lock ./
RUN mkdir -p src/bin && touch src/main.rs src/lib.rs src/bin/bench_nlu.rs build.rs && cargo fetch --locked
COPY build.rs ./
COPY src ./src
COPY tools/compile_rust.py ./tools/compile_rust.py
COPY runtime/src ./runtime/src
ARG LOCAL_UID=1000
RUN useradd -u ${LOCAL_UID} -m clinic && mkdir -p /data /cargo-target && chown -R clinic:clinic /data /cargo-target /workspace /usr/local/cargo
ENV PYTHONPATH=/workspace/runtime/src CARGO_TARGET_DIR=/cargo-target EDITOR_DATA=/data
USER clinic
CMD ["python3", "-m", "clinic.scenario_builder"]

# Default administration image contains no Rust toolchain and no model weights.
FROM python:3.12-slim-bookworm AS editor
WORKDIR /workspace
COPY requirements/editor.txt /workspace/requirements/editor.txt
RUN --mount=type=cache,id=clinic-editor-pip,sharing=locked,target=/root/.cache/pip pip install -r requirements/editor.txt
COPY runtime/src ./runtime/src
COPY scenarios ./scenarios
COPY --from=ui /ui/dist ./scenario-editor/dist
ARG LOCAL_UID=1000
RUN useradd -u ${LOCAL_UID} -m clinic && mkdir -p /data /audio && chown -R clinic:clinic /data /workspace && chown 10001:10001 /audio
ENV PYTHONPATH=/workspace/runtime/src EDITOR_HOST=0.0.0.0 EDITOR_PORT=8090 EDITOR_ALLOW_BUILD=0 EDITOR_BUILD_QUEUE=1 EDITOR_DATA=/data EDITOR_PASSWORD_FILE=/data/admin-password.json
USER clinic
CMD ["python3", "-m", "clinic.scenario_editor"]
