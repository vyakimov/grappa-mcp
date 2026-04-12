#!/usr/bin/env bash
# Render grappa-mcp.service from .env and install it under /etc/systemd/system/.
# Re-run after changing .env to pick up new values.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

if [[ ! -f .env ]]; then
  echo "error: .env not found. Copy .env.example to .env and fill it in first." >&2
  exit 1
fi

set -a
# shellcheck disable=SC1091
source .env
set +a

: "${GRAPPA_MCP_TOKEN:?GRAPPA_MCP_TOKEN must be set in .env}"
: "${MCP_BIND_HOST:?MCP_BIND_HOST must be set in .env}"
: "${MCP_BIND_PORT:?MCP_BIND_PORT must be set in .env}"

USER_NAME="$(id -un)"
RENDERED="$HERE/grappa-mcp.service"

sed \
  -e "s|__USER__|${USER_NAME}|g" \
  -e "s|__INSTALL_DIR__|${HERE}|g" \
  -e "s|__MCP_BIND_HOST__|${MCP_BIND_HOST}|g" \
  -e "s|__MCP_BIND_PORT__|${MCP_BIND_PORT}|g" \
  grappa-mcp.service.example > "$RENDERED"

echo "Rendered unit file: $RENDERED"
echo "Installing to /etc/systemd/system/grappa-mcp.service (requires sudo)..."
sudo install -m 0644 "$RENDERED" /etc/systemd/system/grappa-mcp.service
sudo systemctl daemon-reload
sudo systemctl enable grappa-mcp
sudo systemctl restart grappa-mcp

echo
echo "Service installed. Check status with:"
echo "  systemctl status grappa-mcp"
echo "  journalctl -u grappa-mcp -f"
