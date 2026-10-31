#!/usr/bin/env bash
# Build and run Forge in a locked-down rootless Podman container.
# Usage: ./run.sh [start|build|stop|logs]   (default: start = build + run)
set -euo pipefail
cd "$(dirname "$0")"

IMAGE=forge-job-search
NAME=forge

build() {
    podman build -t "$IMAGE" -f Containerfile .
}

start() {
    if [ ! -f .env ]; then
        echo "Missing .env. Run: cp .env.example .env  (then fill in your keys)" >&2
        exit 1
    fi
    mkdir -p data
    podman rm -f "$NAME" >/dev/null 2>&1 || true
    # Published on loopback only; the single bind mount is ./data -> /data.
    podman run -d --name "$NAME" \
        -p 127.0.0.1:8000:8000 \
        -v "$PWD/data:/data:Z" \
        --userns=keep-id \
        --env-file .env \
        --read-only --tmpfs /tmp \
        --cap-drop=all --security-opt no-new-privileges \
        "$IMAGE"
    echo "Forge is starting at http://127.0.0.1:8000"
}

case "${1:-start}" in
    build) build ;;
    start) build; start ;;
    stop) podman rm -f "$NAME" ;;
    logs) podman logs -f "$NAME" ;;
    *) echo "Usage: $0 [start|build|stop|logs]" >&2; exit 1 ;;
esac
