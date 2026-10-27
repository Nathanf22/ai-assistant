FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY coach ./coach
RUN pip install . && useradd --create-home --uid 1000 coach

USER coach
CMD ["coach-bot"]
