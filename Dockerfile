FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    AUTOCLOCK_DB=/data/autoclock.db \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

RUN pip install --no-cache-dir uv
COPY pyproject.toml uv.lock ./
COPY app ./app
RUN uv sync --frozen --no-dev

RUN useradd -m autoclock && mkdir -p /data && chown autoclock:autoclock /data
USER autoclock
VOLUME /data

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
