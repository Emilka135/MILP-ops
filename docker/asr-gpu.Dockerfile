# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm
WORKDIR /app
COPY requirements /app/requirements
RUN --mount=type=cache,id=clinic-voice-pip-gpu,target=/root/.cache/pip \
    pip install -r requirements/asr-gpu.txt
COPY app /app/app
COPY runtime/src /app/runtime/src
COPY data /app/data
COPY scenarios /app/scenarios
COPY docs/openapi.json /app/docs/openapi.json
RUN chmod -R a+rX /app && useradd -u 10001 -m clinic && mkdir -p /data /debug && chown clinic:clinic /data /debug
ENV PYTHONPATH=/app:/app/runtime/src PYTHONUNBUFFERED=1 HOST=0.0.0.0 PORT=8080 \
    NVIDIA_VISIBLE_DEVICES=all NVIDIA_DRIVER_CAPABILITIES=compute,utility \
    ASR_PROVIDER=CUDAExecutionProvider ASR_GPU_MEM_LIMIT_MIB=1024
USER clinic
CMD ["python", "-m", "clinic", "asr"]
