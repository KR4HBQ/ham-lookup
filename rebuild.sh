#!/bin/bash
# Rebuild and restart the container from this folder (same as `docker compose up -d --build`).
set -e
APP_DIR="${APP_DIR:-$(cd "$(dirname "$0")" && pwd)}"
cd "$APP_DIR"
docker build -t ham-lookup:latest .
docker stop ham-lookup 2>/dev/null || true
docker rm ham-lookup 2>/dev/null || true
docker run -d \
  --name ham-lookup \
  --restart unless-stopped \
  -p 5000:5000 \
  --env-file "$APP_DIR/.env" \
  -v "$APP_DIR/ecr_coasters.json:/app/ecr_coasters.json" \
  -v "$APP_DIR/notable_callsigns.json:/app/notable_callsigns.json" \
  ham-lookup:latest
