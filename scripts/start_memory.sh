#!/usr/bin/env bash
# Sobe o serviço de memória para a coleta. Um worker só: cada worker teria
# o seu próprio buffer e a RAM medida deixaria de ser a do cenário.
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

export MEMORY_MAX_MB="${MEMORY_MAX_MB:-2048}"
export HOST="${HOST:-0.0.0.0}"
export PORT="${PORT:-8002}"
WORKERS="${WORKERS:-1}"

if [[ "$WORKERS" != "1" ]]; then
  echo "aviso: WORKERS=$WORKERS replica o buffer em cada processo. A coleta usa 1 worker." >&2
fi

exec "$UVICORN" services.memory_service.app:app \
  --host "$HOST" \
  --port "$PORT" \
  --workers "$WORKERS" \
  --no-access-log
