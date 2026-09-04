# RecoveryAI backend — agent core + HTTP transport.
#
# The image installs the package properly (`pip install .`) rather than copying
# source onto the PYTHONPATH, so `import recoveryai` behaves identically here and
# in a host's own environment. That matters: this package is meant to be
# embedded, and an image that only works because of a path hack proves nothing.

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependency layer first: source changes far more often than dependencies, and
# splitting them keeps rebuilds fast.
COPY pyproject.toml ./
COPY backend/recoveryai/__init__.py ./backend/recoveryai/__init__.py
RUN pip install --no-cache-dir .

COPY backend/ ./backend/
COPY migrations/ ./migrations/
COPY alembic.ini ./
RUN pip install --no-cache-dir --no-deps .

# Non-root: a container that owns its own filesystem is a container that can be
# turned into a foothold.
RUN useradd --create-home --uid 10001 recoveryai \
    && mkdir -p /app/db \
    && chown -R recoveryai:recoveryai /app
USER recoveryai

# The SQLite file lives on a volume so cases and the LLM daily budget survive a
# container restart. The budget counter in particular is worthless if it resets.
VOLUME ["/app/db"]

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else 1)"

# Migrations run before the server, so a fresh volume comes up with a real
# schema rather than relying on the app's create_all() fallback.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn recoveryai.api.main:app --host 0.0.0.0 --port 8000"]
