#!/usr/bin/env bash
set -euo pipefail
CAMERA_PROJECT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export OPENBLAS_NUM_THREADS=4 OMP_NUM_THREADS=4 PYTHONNOUSERSITE=1
exec "$(dirname -- "${BASH_SOURCE[0]}")/scripts/python.sh" -s "$CAMERA_PROJECT/archivio_gemma/camera_rebuild.py" --index --export "$@"
