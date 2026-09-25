FROM node:22-bookworm-slim AS opencode
RUN npm install -g opencode-ai@1.18.4

FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 agentvisor
COPY --from=opencode /usr/local/bin/node /usr/local/bin/node
COPY --from=opencode /usr/local/lib/node_modules /usr/local/lib/node_modules
COPY --from=opencode /usr/local/bin/opencode /usr/local/bin/opencode
RUN opencode --version
WORKDIR /app
COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY agentvisor ./agentvisor
COPY tests ./tests
COPY launch.py ./
RUN mkdir /data /workspaces && chown agentvisor:agentvisor /data /workspaces
ENV AGENTVISOR_DATA=/data PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 AGENTVISOR_CONTAINER=1 AGENTVISOR_MANAGED_RESTART=1
USER agentvisor
EXPOSE 8420
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8420/api/health', timeout=2)"
CMD ["python", "-m", "agentvisor.server", "--host", "0.0.0.0", "--port", "8420"]
