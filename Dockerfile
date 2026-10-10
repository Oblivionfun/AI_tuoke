FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /uvx /bin/

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY prompts ./prompts
COPY knowledge ./knowledge
COPY seed ./seed
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH" \
    WB_DATABASE_URL=sqlite:////data/workbench.db \
    WB_CSL_PATH=/data/csl.csv \
    TZ=Asia/Shanghai
VOLUME /data
EXPOSE 8000
CMD ["workbench", "serve", "--host", "0.0.0.0", "--port", "8000"]
