#!/usr/bin/with-contenv bashio
set -e

bashio::log.info "Starte Lüftungsgüte-Addon..."

INGRESS_PORT="$(bashio::addon.ingress_port)"
export INGRESS_PORT="${INGRESS_PORT:-8099}"

cd /app
exec python3 -m uvicorn main:app --host 0.0.0.0 --port "${INGRESS_PORT}" --log-level info
