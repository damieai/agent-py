FROM python:3.12-slim
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:0.12.17 /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --frozen --no-dev && useradd --uid 10001 --create-home agent \
    && mkdir -p /app/.runtime && chown agent:agent /app/.runtime
COPY alembic.ini ./
COPY migrations ./migrations
USER agent
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000
CMD ["agent-py", "api", "--host", "0.0.0.0"]
