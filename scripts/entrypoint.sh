#!/usr/bin/env bash
# Universal entrypoint — picks web vs cron mode based on SERVICE_ROLE.
# Used by both the web and cron services on Railway. Web service leaves
# SERVICE_ROLE unset (or "web") and runs the FastAPI dashboard. Cron
# service sets SERVICE_ROLE=cron and runs the FAR pipeline once per
# invocation; Railway's Cron Schedule field controls how often the
# container is started.
set -euo pipefail

role="${SERVICE_ROLE:-web}"

case "$role" in
  cron)
    echo "[entrypoint] starting cron run: flrules run --issues 5 --notify"
    exec flrules run --issues 5 --notify
    ;;
  web|*)
    port="${PORT:-8000}"
    echo "[entrypoint] starting web server on port $port"
    exec uvicorn flrules.web:app --host 0.0.0.0 --port "$port"
    ;;
esac
