# Multi-stage build. The runtime image serves the AgentCore Runtime
# contract (POST /invocations, GET /ping) on port 8080.
# AgentCore Runtime expects an ARM64 image in production; build with
# `docker buildx build --platform linux/arm64` for deployment.
FROM python:3.12-slim AS build
WORKDIR /build
COPY pyproject.toml constraints.txt README.md ./
COPY src ./src
RUN pip install --no-cache-dir --constraint constraints.txt . \
    && pip install --no-cache-dir --constraint constraints.txt uvicorn

FROM python:3.12-slim AS runtime
RUN useradd --create-home --uid 10001 agent
WORKDIR /app
COPY --from=build /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=build /usr/local/bin /usr/local/bin
COPY src ./src
ENV PYTHONPATH=/app/src \
    RETURNS_SQLITE_PATH=/app/data/returns.db \
    RETURNS_EVIDENCE_DIR=/app/data/evidence
RUN mkdir -p /app/data && chown -R agent:agent /app
USER agent
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/ping')"
CMD ["python", "-m", "returns_resolution.runtime_app"]
