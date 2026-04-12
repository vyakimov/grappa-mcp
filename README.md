# grappa-mcp

A [FastMCP](https://github.com/jlowin/fastmcp) server that exposes remote tools — shell execution, file read/write/edit, directory listing, and Docker log inspection — over authenticated HTTP. Designed for agentic clients like Claude Code and OpenAI Codex to drive a remote host without ad hoc SSH.

> **Security warning.** These tools give any authenticated caller arbitrary shell and filesystem access on the host. Never expose this server on a public interface. Bind it to a private VPN (WireGuard, Tailscale, etc.) or loopback, use firewall rules to restrict the source, and keep the bearer token secret.

## Features

| Tool | Description |
|------|-------------|
| `run_command` | Execute a shell command with timeout, env overrides, and output caps |
| `read_file` | Read a text file, with optional offset/limit |
| `write_file` | Atomically create or overwrite a file |
| `edit_file` | Surgical text replacement with optional SHA-256 optimistic concurrency |
| `list_directory` | List entries, optional recursive + glob filter |
| `docker_logs` | Read container logs (`docker logs --tail`, `--since`) |

Bearer-token auth with constant-time comparison, per-command output cap (200 KB), max timeout (120 s), atomic writes, and a denylist of obviously catastrophic commands (`rm -rf /`, `mkfs.*`, etc.).

## Install

Requires Python 3.12+ and [`uv`](https://github.com/astral-sh/uv).

```bash
git clone https://github.com/vyakimov/grappa-mcp.git
cd grappa-mcp
uv sync
cp .env.example .env
# edit .env — set GRAPPA_MCP_TOKEN and MCP_BIND_HOST/MCP_BIND_PORT
```

Generate a strong token:

```bash
python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

## Run

### Directly (development)

```bash
set -a && source .env && set +a
.venv/bin/uvicorn server:app --host "$MCP_BIND_HOST" --port "$MCP_BIND_PORT"
```

### As a systemd service (Linux)

```bash
./install-systemd.sh
```

This renders `grappa-mcp.service` from `grappa-mcp.service.example` using values in `.env`, installs it to `/etc/systemd/system/`, and starts the service. Re-run after editing `.env`.

Inspect with:

```bash
systemctl status grappa-mcp
journalctl -u grappa-mcp -f
```

## Client configuration

### Claude Code (`.mcp.json`)

```json
{
  "mcpServers": {
    "grappa-remote": {
      "type": "http",
      "url": "http://HOST:PORT/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_TOKEN"
      }
    }
  }
}
```

> The `type` field must be `"http"` — Claude Code does not recognize `"streamable-http"`.

### Codex

Add via `codex mcp add` or edit `~/.codex/config.toml`, pointing to the same URL and bearer token.

## Configuration

All configuration is via environment variables, typically set in `.env`:

| Variable | Required | Description |
|----------|----------|-------------|
| `GRAPPA_MCP_TOKEN` | yes | Bearer token clients must present |
| `MCP_BIND_HOST` | for systemd | Host interface for the uvicorn bind (used by `install-systemd.sh`) |
| `MCP_BIND_PORT` | for systemd | Port for the uvicorn bind (used by `install-systemd.sh`) |
| `GRAPPA_MCP_DEFAULT_CWD` | no | Default `cwd` for `run_command` / `list_directory`. Falls back to `$HOME` |
| `GRAPPA_MCP_DEFAULT_DOCKER_CONTAINER` | no | Default container for `docker_logs`. If unset, callers must pass `container` |

## Hardening recommendations

- **Bind to a private interface.** Never `0.0.0.0`. Bind to loopback or a WireGuard/Tailscale address.
- **Add a host firewall rule.** Example (UFW, allow only a specific WireGuard peer):
  ```
  sudo ufw allow from 10.0.0.1 to any port 8020 proto tcp
  ```
- **Run as a non-root user.** The systemd unit runs as the installing user, not root.
- **Rotate the token** if any client disclosed it.

## License

[MIT](LICENSE)
