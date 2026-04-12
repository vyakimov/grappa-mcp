"""Grappa MCP Server — remote tool provider for Claude Code and Codex."""

import asyncio
import hashlib
import hmac
import logging
import os
import sys
import tempfile
from pathlib import Path

from fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

# --- Config ---
MCP_TOKEN = os.environ.get("GRAPPA_MCP_TOKEN", "")
if not MCP_TOKEN:
    print("FATAL: GRAPPA_MCP_TOKEN not set", file=sys.stderr)
    sys.exit(1)

MAX_OUTPUT_BYTES = 200 * 1024  # 200 KB per stream
MAX_TIMEOUT = 120
DEFAULT_CWD = os.environ.get("GRAPPA_MCP_DEFAULT_CWD") or os.path.expanduser("~")
DEFAULT_DOCKER_CONTAINER = os.environ.get("GRAPPA_MCP_DEFAULT_DOCKER_CONTAINER") or None

logger = logging.getLogger("grappa-mcp")

# --- Denylist for obviously catastrophic commands ---
DENY_PATTERNS = [
    "rm -rf /",
    "rm -rf /*",
    "mkfs.",
    "dd if=/dev/zero of=/dev/sd",
    "> /dev/sd",
]

mcp = FastMCP("grappa-mcp")


# --- Auth middleware ---
class BearerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        auth = request.headers.get("authorization", "")
        token = auth.removeprefix("Bearer ").strip()
        if not hmac.compare_digest(token, MCP_TOKEN):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


# --- Helpers ---
def _truncate(data: bytes, limit: int = MAX_OUTPUT_BYTES) -> tuple[str, bool]:
    """Decode bytes, truncate if needed. Returns (text, was_truncated)."""
    if len(data) > limit:
        return data[:limit].decode(errors="replace"), True
    return data.decode(errors="replace"), False


def _file_hash(path: Path) -> str:
    """Return SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _normalize_path(path: str) -> Path:
    """Resolve to absolute path, expanding ~."""
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = Path(DEFAULT_CWD) / p
    return p.resolve()


def _atomic_write(path: Path, content: str, create_dirs: bool = False) -> str:
    """Write content atomically via temp file + rename. Returns file hash."""
    if create_dirs:
        path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.replace(tmp, path)
    except:
        os.unlink(tmp)
        raise
    return _file_hash(path)


# --- Tools ---
@mcp.tool()
async def run_command(
    command: str,
    cwd: str = DEFAULT_CWD,
    timeout_seconds: int = 30,
    env: dict[str, str] | None = None,
) -> dict:
    """Execute a shell command on the host.

    Args:
        command: The shell command to run.
        cwd: Working directory (absolute path). Defaults to GRAPPA_MCP_DEFAULT_CWD or $HOME.
        timeout_seconds: Max execution time, capped at 120s.
        env: Optional environment variable overrides.
    """
    for pattern in DENY_PATTERNS:
        if pattern in command:
            return {"error": f"command matches denylist pattern: {pattern}"}

    timeout_seconds = min(timeout_seconds, MAX_TIMEOUT)
    cwd_path = Path(cwd)
    if not cwd_path.is_dir():
        return {"error": f"cwd does not exist: {cwd}"}

    proc_env = os.environ.copy()
    if env:
        proc_env.update(env)

    timed_out = False
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=proc_env,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_seconds
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        stdout = b""
        stderr = b""
        timed_out = True

    stdout_text, stdout_truncated = _truncate(stdout)
    stderr_text, stderr_truncated = _truncate(stderr)

    return {
        "exit_code": proc.returncode if not timed_out else -1,
        "stdout": stdout_text,
        "stderr": stderr_text,
        "timed_out": timed_out,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
    }


@mcp.tool()
async def read_file(
    path: str,
    offset: int = 0,
    limit: int | None = None,
) -> dict:
    """Read a text file on the host.

    Args:
        path: Absolute or relative file path.
        offset: Character offset to start reading from.
        limit: Maximum number of characters to read.
    """
    p = _normalize_path(path)
    if not p.is_file():
        return {"error": f"file not found: {p}"}

    try:
        text = p.read_text()
    except Exception as e:
        return {"error": str(e)}

    total_chars = len(text)
    text = text[offset:]
    truncated = False
    if limit is not None and len(text) > limit:
        text = text[:limit]
        truncated = True

    return {
        "content": text,
        "total_chars": total_chars,
        "offset": offset,
        "truncated": truncated,
    }


@mcp.tool()
async def write_file(
    path: str,
    content: str,
    overwrite: bool = False,
    create_dirs: bool = False,
) -> dict:
    """Create or fully overwrite a file on the host.

    Args:
        path: Absolute or relative file path.
        content: The full file content to write.
        overwrite: If false, refuse to overwrite existing files.
        create_dirs: If true, create parent directories as needed.
    """
    p = _normalize_path(path)
    if p.is_dir():
        return {"error": f"path is a directory: {p}"}
    if p.exists() and not overwrite:
        return {"error": f"file already exists (set overwrite=true): {p}"}

    try:
        sha256 = _atomic_write(p, content, create_dirs=create_dirs)
    except Exception as e:
        return {"error": str(e)}

    return {"path": str(p), "sha256": sha256}


@mcp.tool()
async def edit_file(
    path: str,
    old_text: str = "",
    new_text: str = "",
    replace_all: bool = False,
    expected_sha256: str | None = None,
    # Aliases — Claude Code's Edit tool uses old_string/new_string
    old_string: str = "",
    new_string: str = "",
) -> dict:
    """Surgical text replacement in an existing file on the host.

    Args:
        path: Absolute or relative file path.
        old_text: The exact text to find and replace (alias: old_string).
        new_text: The replacement text (alias: new_string).
        replace_all: If false, fail when multiple matches are found.
        expected_sha256: Optional SHA-256 hash of the file before editing; fails on mismatch.
    """
    # Support old_string/new_string as aliases for old_text/new_text
    old_text = old_text or old_string
    new_text = new_text or new_string
    if not old_text:
        return {"error": "old_text (or old_string) is required"}
    p = _normalize_path(path)
    if not p.is_file():
        return {"error": f"file not found: {p}"}

    try:
        content = p.read_text()
    except Exception as e:
        return {"error": str(e)}

    if expected_sha256:
        actual_hash = _file_hash(p)
        if not hmac.compare_digest(actual_hash, expected_sha256):
            return {
                "error": "sha256 mismatch — file was modified",
                "actual_sha256": actual_hash,
            }

    count = content.count(old_text)
    if count == 0:
        return {"error": "old_text not found in file"}
    if count > 1 and not replace_all:
        return {"error": f"ambiguous: old_text found {count} times (set replace_all=true)"}

    if replace_all:
        new_content = content.replace(old_text, new_text)
    else:
        new_content = content.replace(old_text, new_text, 1)

    try:
        sha256 = _atomic_write(p, new_content)
    except Exception as e:
        return {"error": str(e)}

    return {"path": str(p), "replacements": count if replace_all else 1, "sha256": sha256}


@mcp.tool()
async def list_directory(
    path: str = DEFAULT_CWD,
    recursive: bool = False,
    glob: str | None = None,
) -> dict:
    """List files and directories on the host.

    Args:
        path: Directory path to list.
        recursive: If true, list recursively.
        glob: Optional glob pattern to filter entries.
    """
    p = _normalize_path(path)
    if not p.is_dir():
        return {"error": f"directory not found: {p}"}

    try:
        if glob:
            entries = list(p.rglob(glob) if recursive else p.glob(glob))
        elif recursive:
            entries = list(p.rglob("*"))
        else:
            entries = list(p.iterdir())

        max_entries = 2000
        truncated = len(entries) > max_entries
        entries = sorted(entries[:max_entries])

        result = []
        for e in entries:
            result.append({
                "name": str(e.relative_to(p)),
                "type": "dir" if e.is_dir() else "file",
                "size": e.stat().st_size if e.is_file() else None,
            })
    except Exception as e:
        return {"error": str(e)}

    return {"path": str(p), "entries": result, "truncated": truncated}


@mcp.tool()
async def docker_logs(
    container: str | None = None,
    tail: int = 200,
    since: str | None = None,
) -> dict:
    """Read Docker container logs on the host.

    Args:
        container: Container name. Falls back to GRAPPA_MCP_DEFAULT_DOCKER_CONTAINER.
        tail: Number of lines from the end. Defaults to 200.
        since: Optional duration string (e.g. '10m', '1h').
    """
    container = container or DEFAULT_DOCKER_CONTAINER
    if not container:
        return {"error": "container is required (or set GRAPPA_MCP_DEFAULT_DOCKER_CONTAINER)"}
    cmd = ["docker", "logs", "--tail", str(tail)]
    if since:
        cmd.extend(["--since", since])
    cmd.append(container)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return {"error": "docker logs timed out"}

    stdout_text, stdout_truncated = _truncate(stdout)
    stderr_text, stderr_truncated = _truncate(stderr)

    return {
        "container": container,
        "stdout": stdout_text,
        "stderr": stderr_text,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
    }


# --- App setup ---
app = mcp.http_app()
app.add_middleware(BearerAuthMiddleware)
