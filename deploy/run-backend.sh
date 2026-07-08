#!/usr/bin/env bash
# Launch the booth backend. Enables HTTPS automatically when a cert/key pair exists
# in certs/ (created by gen-cert.sh); otherwise serves plain HTTP.
set -euo pipefail
APP="${APP:-/opt/photobooth}"
cd "$APP"

# Unbuffered output so logs ([hub] lines etc.) appear live under systemd instead of
# sitting in a block buffer.
export PYTHONUNBUFFERED=1

PORT="${BOOTH_PORT:-8000}"
CERT="$APP/certs/cert.pem"
KEY="$APP/certs/key.pem"
ARGS=(--host 0.0.0.0 --port "$PORT")

if [ -f "$CERT" ] && [ -f "$KEY" ]; then
  export BOOTH_SCHEME=https
  ARGS+=(--ssl-certfile "$CERT" --ssl-keyfile "$KEY")
  echo "[run-backend] HTTPS enabled on :$PORT"
else
  export BOOTH_SCHEME=http
  echo "[run-backend] HTTP on :$PORT (no certs/ — run gen-cert.sh for HTTPS)"
fi

exec "$APP/venv/bin/uvicorn" backend.main:app "${ARGS[@]}"
