#!/usr/bin/env bash
set -euo pipefail
CAMERA_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CAMERA_CORPUS="$CAMERA_ROOT/data/camera_deputati/testi_tematici"
if [[ ! -d "$CAMERA_CORPUS" ]]; then CAMERA_CORPUS="$CAMERA_ROOT/data/camera_deputati/testi"; fi
exec "$CAMERA_ROOT/Avvia-GemmaDesk.sh" \
    --data "$CAMERA_ROOT/data/camera_deputati/database" \
    --corpus "$CAMERA_CORPUS" \
    --collection 'Camera dei deputati' "$@"
