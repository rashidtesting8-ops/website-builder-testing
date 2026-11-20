FROM mcr.microsoft.com/playwright/python:v1.55.0-noble
WORKDIR /workspace
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/workspace/src:/workspace
# Pinned lock exported from uv.lock (`uv export --frozen --all-extras --no-dev
# --no-emit-project --no-hashes`). CI verifies this file stays in sync.
COPY requirements.lock.txt ./
RUN pip install --no-cache-dir -r requirements.lock.txt
COPY src ./src
COPY app ./app
COPY config ./config
EXPOSE 8787
CMD ["uvicorn", "app.service:app", "--host", "0.0.0.0", "--port", "8787"]
