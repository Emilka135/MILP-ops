# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm AS runtime
WORKDIR /app
ENV PIP_DEFAULT_TIMEOUT=20 PIP_RETRIES=1
COPY requirements/base.txt /app/requirements/base.txt
RUN --mount=type=cache,id=clinic-voice-pip-base,sharing=locked,target=/root/.cache/pip pip install -r requirements/base.txt
COPY requirements/asr.txt requirements/tts.txt requirements/nlu.txt requirements/nlu-semantic.txt /app/requirements/
ARG MODULE=crm
ARG WITH_ML=0
RUN --mount=type=cache,id=clinic-voice-pip-nlu,sharing=locked,target=/root/.cache/pip if [ "$MODULE" = "nlu" ]; then pip install -r requirements/nlu-semantic.txt; fi
RUN --mount=type=cache,id=clinic-voice-pip-modules,sharing=locked,target=/root/.cache/pip \
    if [ "$WITH_ML" = "1" ]; then \
      if [ "$MODULE" = "tts" ] || [ "$MODULE" = "nlu" ]; then pip install --index-url https://download.pytorch.org/whl/cpu torch==2.14.1; fi; \
      pip install -r requirements/${MODULE}.txt; \
    fi
RUN if [ "$MODULE" = "tts" ]; then apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*; fi
COPY app /app/app
COPY runtime/src /app/runtime/src
COPY data /app/data
COPY scenarios /app/scenarios
COPY docs/openapi.json /app/docs/openapi.json
RUN chmod -R a+rX /app && useradd -u 10001 -m clinic && mkdir -p /data /audio && chown clinic:clinic /data /audio
ENV PYTHONPATH=/app:/app/runtime/src PYTHONUNBUFFERED=1 HOST=0.0.0.0 PORT=8080
ENV SERVICE_MODULE=${MODULE}
USER clinic
CMD ["sh", "-c", "exec python -m clinic \"$SERVICE_MODULE\""]
