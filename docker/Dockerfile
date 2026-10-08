FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_NO_CACHE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:${PATH}"

WORKDIR /app
COPY . /app

ARG MOCK_RANDOM_SEED=20260923
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG UV_DEFAULT_INDEX=https://pypi.org/simple
ENV MOCK_RANDOM_SEED=${MOCK_RANDOM_SEED}

# uv.lock pins the application dependencies. Rebuild the synthetic data inside
# the Linux image so the SQLite file and manifest always match that image.
RUN python -m pip install --no-cache-dir uv==0.11.19 \
    && uv sync --frozen --no-dev \
    && python scripts/generate_seed_data.py \
    && python scripts/verify_data_manifest.py \
    && groupadd --system agent \
    && useradd --system --gid agent --home-dir /app agent \
    && mkdir -p /app/runtime \
    && chown -R agent:agent /app/runtime /app/knowledge

USER agent
EXPOSE 8000 8081

CMD ["python", "-m", "uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
