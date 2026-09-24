# CatalystEdge backend: one image for the API, the Celery worker and the Celery beat scheduler.
# Python packages are installed at the versions pinned in uv.lock. PyTorch comes from the
# CPU-only index (much smaller than the default CUDA build); set INSTALL_ML=false to skip the
# sentiment model libraries entirely (the app then uses the labelled word-list fallback).
FROM python:3.12-slim AS base

ARG INSTALL_ML=true
# TimesFM's Python package (small). Its ~1.3 GB weights download into the models volume only
# when you switch TimesFM on in Settings. Set false to leave it out entirely.
ARG INSTALL_TIMESFM=true
ARG TORCH_VERSION=2.14.0
ARG TORCH_INDEX=https://download.pytorch.org/whl/cpu

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MODELS_DIR=/models \
    HF_HOME=/models/hf-cache \
    BACKTEST_CACHE=/models/backtest-cache \
    REPORTS_DIR=/models/reports

WORKDIR /app
RUN pip install uv==0.8.17

COPY pyproject.toml uv.lock ./
RUN EXTRAS="--extra ml$( [ "$INSTALL_TIMESFM" = "true" ] && echo ' --extra timeseries' )" \
 && uv export --frozen --no-dev $EXTRAS --no-emit-project --no-hashes -o /tmp/req.txt \
 && grep -v -E '^(torch|triton|nvidia-)' /tmp/req.txt > /tmp/req-notorch.txt \
 && uv pip install --system -r /tmp/req-notorch.txt \
 && if [ "$INSTALL_ML" = "true" ]; then uv pip install --system "torch==${TORCH_VERSION}" --index-url "${TORCH_INDEX}"; fi

COPY alembic.ini ./
COPY alembic ./alembic
COPY catalystedge ./catalystedge
RUN uv pip install --system --no-deps . \
 && useradd --create-home --uid 10001 app \
 && mkdir -p /models && chown app /models
USER app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health').status==200 else 1)" || exit 1
CMD ["uvicorn", "catalystedge.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
