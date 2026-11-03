#!/usr/bin/env bash
set -euo pipefail

python -m pip install -e '.[dev]'
mkdir -p data/storage
python -m app.cli migrate
echo "Video AI Studio ready. Run: uvicorn app.main:app --reload"
