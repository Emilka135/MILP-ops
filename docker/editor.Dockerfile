FROM node:26-bookworm-slim AS ui
WORKDIR /ui
COPY scenario-editor/package*.json ./
RUN npm ci
COPY scenario-editor/index.html scenario-editor/vite.config.js ./
COPY scenario-editor/src ./src
RUN npm run build
FROM rust:1.98-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends python3 ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /workspace
COPY Cargo.toml Cargo.lock build.rs ./
COPY src ./src
COPY tools/compile_rust.py ./tools/compile_rust.py
COPY runtime/src ./runtime/src
COPY scenarios ./scenarios
COPY --from=ui /ui/dist ./scenario-editor/dist
RUN cargo fetch --locked
ARG LOCAL_UID=1000
RUN useradd -u ${LOCAL_UID} -m clinic && mkdir -p /data /workspace/target && chown -R clinic:clinic /data /workspace /usr/local/cargo
ENV PYTHONPATH=/workspace/runtime/src EDITOR_HOST=0.0.0.0 EDITOR_PORT=8090 EDITOR_ALLOW_BUILD=1 EDITOR_DATA=/data EDITOR_PASSWORD_FILE=/data/admin-password.json
USER clinic
CMD ["python3", "-m", "clinic.scenario_editor"]
