FROM ghcr.io/astral-sh/uv:0.9.27-python3.12-bookworm-slim AS build
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev
COPY src src

FROM python:3.12.12-slim-bookworm
RUN useradd --create-home --uid 10001 app
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY src src
ENV PATH="/app/.venv/bin:$PATH" PYTHONPATH="/app/src" AGENT_DATA_DIR="/data"
RUN mkdir /data && chown app:app /data
USER app
EXPOSE 8000
CMD ["uvicorn", "resilient_agent.app:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
