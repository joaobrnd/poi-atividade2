#!/usr/bin/env bash
# Sobe o serviço de CPU. O padrão é um worker por vCPU: o cálculo é Python
# puro e um único processo não passa de um núcleo.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -x "$ROOT/.venv/bin/uvicorn" ]]; then
  UVICORN="$ROOT/.venv/bin/uvicorn"
elif command -v uvicorn >/dev/null 2>&1; then
  UVICORN="uvicorn"
else
  echo "uvicorn não encontrado. Crie o virtualenv e instale requirements.txt." >&2
  exit 1
fi

export HOST="${HOST:-0.0.0.0}"
export PORT="${PORT:-8001}"
WORKERS="${WORKERS:-$(nproc)}"

exec "$UVICORN" services.cpu_service.app:app \
  --host "$HOST" \
  --port "$PORT" \
  --workers "$WORKERS" \
  --no-access-log
