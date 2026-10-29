#!/usr/bin/env bash
set -euo pipefail
GEMMAVIDEO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec "$GEMMAVIDEO_ROOT/scripts/python.sh" "$GEMMAVIDEO_ROOT/video_gemma/app.py" "$@"
