# Python 3.12 rather than 3.14: every pinned wheel (asyncpg, pydantic-core,
# greenlet) ships a prebuilt cp312 binary, so the image builds without a
# compiler. TECH_SPEC §2 requires 3.11+, so this is well within spec.
FROM python:3.12-slim

# Unbuffered stdout so `docker-compose logs -f` shows output as it happens
# rather than in delayed blocks.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Requirements first: this layer is cached unless the pins change, so ordinary
# source edits do not reinstall the dependency tree.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run as a non-root user.
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Default command runs the API. docker-compose overrides it for the worker
# service, which shares this image but runs as a separate process (TECH_SPEC §8.2).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
