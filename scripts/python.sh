#!/usr/bin/env bash
set -euo pipefail
GEMMA_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-4}" OPENBLAS_NUM_THREADS="${OPENBLAS_NUM_THREADS:-4}" PYTHONNOUSERSITE=1
export NUMBA_CACHE_DIR="${NUMBA_CACHE_DIR:-$GEMMA_ROOT/.local/numba-cache}"
if [[ -n "${GEMMA_PYTHON:-}" ]]; then
    exec "$GEMMA_PYTHON" "$@"
elif [[ -x "$GEMMA_ROOT/.venv/bin/python" ]]; then
    exec "$GEMMA_ROOT/.venv/bin/python" "$@"
else
    exec python3 "$@"
fi
