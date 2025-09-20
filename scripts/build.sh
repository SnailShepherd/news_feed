#!/usr/bin/env bash
set -euo pipefail
python -m newsfeed build --window-start "${1:-}" --out docs/unified.json
