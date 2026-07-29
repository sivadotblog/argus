FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build
WORKDIR /app
COPY pyproject.toml ./
COPY uv.lock ./
COPY src/ ./src/
RUN uv pip install --system --no-cache --compile-bytecode -r /dev/stdin < <(uv export --frozen --no-dev)

FROM python:3.12-slim
RUN useradd --uid 10001 --no-create-home app
COPY --from=build /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=build /usr/local/bin/healthcheck-engine /usr/local/bin/healthcheck-engine
USER 10001
# PYTHONUNBUFFERED is critical: stdout must not buffer or OTEL sees results late
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["healthcheck-engine"]
