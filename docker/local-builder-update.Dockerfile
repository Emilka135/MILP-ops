# Refresh the project's existing offline compiler image and keep its toolchain.
ARG BASE_IMAGE=clinic-voice-local-base:scenario-builder
FROM ${BASE_IMAGE}
COPY runtime/src /workspace/runtime/src
COPY src /workspace/src
COPY tools /workspace/tools
COPY Cargo.toml Cargo.lock build.rs /workspace/
COPY scenarios /workspace/scenarios
