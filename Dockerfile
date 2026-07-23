FROM python:3.13-slim

ARG AGENT_REMOTE_VERSION=0.0.4-fix.5
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/src
ENV AGENT_REMOTE_VERSION=$AGENT_REMOTE_VERSION

LABEL org.opencontainers.image.version=$AGENT_REMOTE_VERSION

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
