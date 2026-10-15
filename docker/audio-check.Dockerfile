FROM python:3.12-slim-bookworm
WORKDIR /app
COPY runtime/src /app/runtime/src
ENV PYTHONPATH=/app/runtime/src PYTHONUNBUFFERED=1
USER 10001:10001
CMD ["python", "-m", "clinic.audio_check"]
