FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.5.30 /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends git libxrender1 libxext6 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev
COPY bench ./bench
ENV PATH="/app/.venv/bin:$PATH"
ENTRYPOINT ["python"]
