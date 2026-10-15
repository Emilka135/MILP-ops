FROM python:3.12-slim-bookworm AS runtime
ARG MODULE=crm
ARG WITH_ML=0
WORKDIR /app
COPY requirements /app/requirements
RUN pip install --no-cache-dir -r requirements/base.txt && \
    if [ "$WITH_ML" = "1" ]; then \
      if [ "$MODULE" = "tts" ] || [ "$MODULE" = "nlu" ]; then pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch==2.14.1; fi; \
      pip install --no-cache-dir -r requirements/${MODULE}.txt; \
    fi
COPY app /app/app
COPY runtime/src /app/runtime/src
COPY data /app/data
COPY docs/openapi.json /app/docs/openapi.json
RUN useradd -u 10001 -m clinic && mkdir -p /data /audio && chown clinic:clinic /data /audio
ENV PYTHONPATH=/app:/app/runtime/src PYTHONUNBUFFERED=1 HOST=0.0.0.0 PORT=8080
ENV SERVICE_MODULE=${MODULE}
USER clinic
CMD ["sh", "-c", "exec python -m clinic \"$SERVICE_MODULE\""]
