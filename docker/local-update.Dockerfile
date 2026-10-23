# Offline update of an already accepted project runtime; no dependency changes.
ARG BASE_IMAGE=clinic-voice-local-base:crm
FROM ${BASE_IMAGE}
ARG SOURCE_ROOT=/app
COPY runtime/src ${SOURCE_ROOT}/runtime/src
COPY scenario-editor/dist ${SOURCE_ROOT}/scenario-editor/dist

COPY scenarios ${SOURCE_ROOT}/scenarios
