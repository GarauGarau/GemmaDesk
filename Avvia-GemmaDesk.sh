#!/usr/bin/env bash
set -euo pipefail
GEMMADESK_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec "$GEMMADESK_ROOT/scripts/python.sh" "$GEMMADESK_ROOT/archivio_gemma/app.py" "$@"
