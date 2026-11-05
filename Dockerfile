FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

# Editable install: the package stays in /app/src, so the project root (where
# .env and descriptions.json are looked for) is /app.
RUN pip install -e . && useradd --create-home --uid 1000 app

USER app

ENTRYPOINT ["postgres-mcp-server"]
