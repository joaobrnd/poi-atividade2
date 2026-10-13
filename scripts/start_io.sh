#!/usr/bin/env bash
# Sobe o serviço de IO fora de /tmp, que em várias instalações é tmpfs (RAM).
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

export IO_DATA_DIR="${IO_DATA_DIR:-$ROOT/.data/io}"
mkdir -p "$IO_DATA_DIR"
export HOST="${HOST:-0.0.0.0}"
export PORT="${PORT:-8003}"
WORKERS="${WORKERS:-1}"

exec "$UVICORN" services.io_service.app:app \
  --host "$HOST" \
  --port "$PORT" \
  --workers "$WORKERS" \
  --no-access-log
