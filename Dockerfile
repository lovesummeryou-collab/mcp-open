FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN useradd --create-home --shell /bin/bash appuser

COPY --chown=appuser:appuser requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY --chown=appuser:appuser . .

RUN mkdir -p /app/data \
    && chown -R appuser:appuser /app \
    && chmod -R a+rX /app \
    && chmod +x /app/run.py

USER appuser

ENV MCPOPEN_HOST=0.0.0.0 \
    MCPOPEN_PORT=8765 \
    MCPOPEN_DB=/app/data/mcp-open.db

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=3)"

CMD ["python", "/app/run.py"]
