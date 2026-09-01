FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PYTHONPATH=/app
WORKDIR /app
COPY apps/api/pyproject.toml apps/api/README.md ./
RUN pip install --upgrade pip && pip install -e .
COPY apps/api ./
RUN pip install -e . && addgroup --system sidra && adduser --system --ingroup sidra sidra \
    && mkdir -p /app/logs && chown -R sidra:sidra /app
USER sidra
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
