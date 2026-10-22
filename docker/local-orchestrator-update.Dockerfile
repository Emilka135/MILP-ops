# Offline source delivery over the project's already installed runtime.
ARG BASE_IMAGE=clinic-voice-local-base:orchestrator
FROM ${BASE_IMAGE}
COPY --chmod=755 target/release/clinic-voice /usr/local/bin/clinic-voice
