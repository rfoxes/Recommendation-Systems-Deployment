# syntax=docker/dockerfile:1
FROM python:3.13-slim

# uv installs dependencies exactly as pinned in uv.lock
COPY --from=ghcr.io/astral-sh/uv:0.11.21 /uv /bin/uv

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# LightGBM needs the OpenMP runtime
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 && rm -rf /var/lib/apt/lists/*

# Dependencies first: this layer stays cached until pyproject.toml / uv.lock change
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Code and runtime assets last, since they change most often
COPY app ./app
COPY data ./data
COPY template ./template
COPY prompts ./prompts

# CTR model: downloaded from the public model repo's pinned release and checksum-verified at build time
RUN python -m app.ranking.model_files

RUN useradd --create-home appuser
USER appuser

# Cloud Run injects $PORT; 8080 is the default when running locally
ENV PORT=8080
EXPOSE 8080

# Shell form so ${PORT} expands; exec makes uvicorn PID 1 so it receives SIGTERM
CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT}
