FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PYTHONPATH=/app
WORKDIR /app
COPY apps/api/pyproject.toml apps/api/README.md ./
RUN pip install --upgrade pip && pip install -e .
COPY apps/api ./
RUN pip install -e . && addgroup --system sidra && adduser --system --ingroup sidra sidra \
    && mkdir -p /app/logs /app/research_data && chown -R sidra:sidra /app
# The commit this image was built from, read by app/services/build_info.py and
# compared across containers by scripts/verify-deployment.sh. Declared after
# the installs so a new commit rebuilds only this layer. Pass it at deploy:
#   SIDRA_BUILD_COMMIT=$(git rev-parse HEAD) docker compose up -d --build ...
# Never set it in .env: env_file would override the value baked in here.
ARG SIDRA_BUILD_COMMIT=unknown
ENV SIDRA_BUILD_COMMIT=$SIDRA_BUILD_COMMIT
USER sidra
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
