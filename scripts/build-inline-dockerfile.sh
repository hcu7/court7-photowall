#!/usr/bin/env bash
#
# Erzeugt ein "self-contained" Deploy-Dockerfile: der App-Code (main.py,
# requirements.txt, static/) wird als gzip+base64 in einen RUN-Befehl
# eingebacken, inkl. SHA-256-Integritätscheck. Dadurch baut das Image OHNE
# GitHub-Zugriff — gedacht für das private Coolify-Deployment via
# coolify-MCP (create_application_from_dockerfile), wo Coolify das (private)
# Repo nicht selbst klonen kann.
#
# Das Repo bleibt Source-of-Truth; dieses Script macht den Deploy reproduzierbar.
#
# Nutzung:  scripts/build-inline-dockerfile.sh > /tmp/inline.Dockerfile
#
set -euo pipefail
cd "$(dirname "$0")/.."

TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT
tar czf "$TMP" main.py requirements.txt static/

B64="$(base64 < "$TMP" | tr -d '\n')"
if command -v sha256sum >/dev/null 2>&1; then
  SHA="$(sha256sum "$TMP" | awk '{print $1}')"
else
  SHA="$(shasum -a 256 "$TMP" | awk '{print $1}')"
fi

cat <<DOCKERFILE
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
RUN echo '${B64}' | base64 -d > /tmp/app.tgz && echo '${SHA}  /tmp/app.tgz' | sha256sum -c && tar xzf /tmp/app.tgz && rm /tmp/app.tgz
RUN pip install --no-cache-dir -r requirements.txt
RUN mkdir -p /data/uploads /data/hidden
ENV DATA_DIR=/data
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD curl -fsS http://localhost:8000/healthz || exit 1
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
DOCKERFILE
