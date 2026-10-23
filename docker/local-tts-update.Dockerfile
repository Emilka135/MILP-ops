# Same MIT accentor as requirements/tts.txt, installed in own model cache.
ARG BASE_IMAGE=clinic-voice-local-base:tts
FROM ${BASE_IMAGE}
COPY modules/tts/models/accentor /opt/clinic-accentor
ENV PYTHONPATH=/opt/clinic-accentor:/app:/app/runtime/src
COPY runtime/src /app/runtime/src

COPY scenarios /app/scenarios
