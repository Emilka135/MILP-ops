FROM python:3.12-slim-bookworm
CMD ["python", "-c", "import os; os.chown('/audio',10001,10001); os.chmod('/audio',0o755)"]
