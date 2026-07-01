#!/usr/bin/env bash
#
# Launches llama-server with whichever model Hannah currently has selected.
# The selection lives in logs/selected_model (set via the web UI) and falls back
# to config.json's "model". This is the ExecStart for hannah-llama.service, so a
# model switch is just: update the selection, then restart that service.
#
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

# CUDA runtime libs (Jetson); harmless if already in the linker cache.
export LD_LIBRARY_PATH=/usr/local/cuda/lib64

MODEL=$(/usr/bin/python3 -c "import hannah; print(hannah.selected_model_path())")
CTX=$(/usr/bin/python3 -c "import hannah; print(hannah.server_ctx())")

echo "Starting llama-server with model: $MODEL (ctx $CTX)"
exec "$HOME/src/llama.cpp/build/bin/llama-server" \
    -m "$MODEL" -ngl 99 -c "$CTX" --host 127.0.0.1 --port 8080
