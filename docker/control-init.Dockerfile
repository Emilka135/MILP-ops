FROM python:3.12-slim-bookworm
COPY runtime/src/clinic/control_init.py /control_init.py
CMD ["python", "/control_init.py"]
