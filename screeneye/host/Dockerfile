# ScreenEye host: the push receiver (the container's main process) and the
# ScreenEye MCP, which is run inside it with `docker exec -i`.
#
# Build from screeneye/, so both packages are in the context:
#   docker build -f host/Dockerfile -t screeneye-host .
ARG BASE=python:3.13-slim
FROM ${BASE}

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY host /src/host
COPY mcp /src/mcp
RUN pip install /src/host /src/mcp && rm -rf /src

# The share is mounted with uid/gid 1000 (nakomis on the host).
USER 1000:1000
ENV SCREENEYE_ROOT=/mnt/print-recordings \
    SCREENEYE_HOST_PORT=8090 \
    SCREENEYE_DIR=/mnt/print-recordings/screeneye/snapshots
EXPOSE 8090
HEALTHCHECK --interval=60s --timeout=5s \
  CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ['SCREENEYE_HOST_PORT'], timeout=4)"]
CMD ["screeneye-host"]
