FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/src

WORKDIR /app

COPY pyproject.toml alembic.ini README.md LICENSE ./
COPY src ./src
COPY migrations ./migrations
COPY scripts/docker-entrypoint.sh ./scripts/docker-entrypoint.sh

RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir . \
    && chmod +x ./scripts/docker-entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["./scripts/docker-entrypoint.sh"]
CMD ["uvicorn", "agent_remote_server.main:app", "--host", "0.0.0.0", "--port", "8000"]
