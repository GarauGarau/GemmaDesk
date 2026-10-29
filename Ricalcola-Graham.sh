#!/usr/bin/env bash
set -euo pipefail
GRAHAM_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export OMP_NUM_THREADS=4 OPENBLAS_NUM_THREADS=4 NUMBA_NUM_THREADS=12 PYTHONNOUSERSITE=1
exec "$(dirname -- "${BASH_SOURCE[0]}")/scripts/python.sh" -s -u "$GRAHAM_ROOT/archivio_gemma/essay_rebuild.py" "$@"
