"""Grappa MCP Server — remote tool provider for Claude Code and Codex."""

import asyncio
import errno
import fnmatch
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import signal
import stat as statmod
import tempfile
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

# --- Config ---
MAX_OUTPUT_BYTES = 200 * 1024  # 200 KB per stream
MAX_TIMEOUT = 120
MAX_RUNNING_JOBS = 20  # concurrent background jobs
MAX_FINISHED_JOBS = 50  # finished jobs kept for job_status before eviction
MAX_JOB_WAIT = 60  # cap on job_status wait_seconds
MAX_READ_BYTES = 50 * 1024 * 1024  # refuse read_file on anything larger
MAX_SEARCH_FILE_BYTES = 2 * 1024 * 1024  # search_files skips larger files
DEFAULT_CWD = os.environ.get("GRAPPA_MCP_DEFAULT_CWD") or os.path.expanduser("~")
DEFAULT_DOCKER_CONTAINER = os.environ.get("GRAPPA_MCP_DEFAULT_DOCKER_CONTAINER") or None
LOG_LEVEL = os.environ.get("GRAPPA_MCP_LOG_LEVEL", "INFO")

logger = logging.getLogger("grappa-mcp")


def _load_tokens() -> tuple[str, ...]:
    """Parse GRAPPA_MCP_TOKEN as a comma-separated token list (allows rotation)."""
    raw = os.environ.get("GRAPPA_MCP_TOKEN", "")
    return tuple(t.strip() for t in raw.split(",") if t.strip())


# --- Denylist for obviously catastrophic commands ---
# This is a tripwire against accidents, not a security boundary: any
# authenticated caller has full shell access regardless. Patterns are anchored
# so that e.g. `rm -rf /tmp/build` is NOT blocked, only rm aimed at `/` itself.
DENY_PATTERNS = [
    re.compile(r"\brm\s+(?:-{1,2}[\w=-]+\s+)*/+\*?\s*(?:-{1,2}[\w=-]+\s*)*(?:$|[;&|)])"),
    re.compile(r"\bmkfs(?:\.\w+)?\b"),
    re.compile(r"\bdd\b[^;|&]*\bof=/dev/(?:sd|hd|vd|nvme|mmcblk)"),
    re.compile(r">\s*/dev/(?:sd|hd|vd|nvme|mmcblk)"),
]

# Directories search_files never descends into.
SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "node_modules",
    "venv",
}

_CONTAINER_NAME_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*")

mcp = FastMCP("grappa-mcp")


# --- Auth middleware ---
class BearerAuthMiddleware:
    """Pure ASGI bearer-token auth.

    Deliberately not BaseHTTPMiddleware: that wrapper buffers responses
    through a task and is known to misbehave with the SSE streams the MCP
    HTTP transport uses.
    """

    def __init__(
        self,
        app: ASGIApp,
        tokens: tuple[str, ...],
        open_paths: frozenset[str] = frozenset({"/healthz"}),
    ):
        self.app = app
        self._tokens = tuple(t.encode() for t in tokens)
        self._open_paths = open_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in self._open_paths:
            await self.app(scope, receive, send)
            return
        auth = b""
        for name, value in scope["headers"]:
            if name == b"authorization":
                auth = value
                break
        token = auth.removeprefix(b"Bearer ").strip()
        if not any(hmac.compare_digest(token, t) for t in self._tokens):
            response = JSONResponse({"error": "unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


# --- Helpers ---
def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_hash(path: Path) -> str:
    """Return SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _normalize_path(path: str) -> Path:
    """Resolve to absolute path, expanding ~ and following symlinks."""
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = Path(DEFAULT_CWD) / p
    return p.resolve()


def _normalize_path_keep_link(path: str) -> Path:
    """Like _normalize_path, but does not resolve a final symlink component.

    Used by tools that must act on the link itself (stat, delete, move).
    """
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = Path(DEFAULT_CWD) / p
    return p.parent.resolve() / p.name


def _current_umask() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return mask


def _atomic_write(path: Path, content: str, create_dirs: bool = False) -> str:
    """Write content atomically via temp file + rename. Returns content hash.

    Preserves the permissions of an existing file (mkstemp defaults to 0600,
    which would otherwise leak onto the target on rename).
    """
    if create_dirs:
        path.parent.mkdir(parents=True, exist_ok=True)
    elif not path.parent.is_dir():
        raise FileNotFoundError(
            f"parent directory does not exist: {path.parent} (set create_dirs=true)"
        )
    try:
        mode = statmod.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        mode = 0o666 & ~_current_umask()
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    return _sha256(content.encode("utf-8"))


class _StreamBuffer:
    """Drains a subprocess stream, keeping at most `limit` bytes.

    Keeps reading past the limit so the child never blocks on a full pipe,
    and the collected data stays accessible even if drain() is cancelled.
    """

    def __init__(self, limit: int):
        self._chunks: list[bytes] = []
        self._kept = 0
        self._total = 0
        self._limit = limit

    async def drain(self, stream: asyncio.StreamReader) -> None:
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                return
            self._total += len(chunk)
            if self._kept < self._limit:
                take = chunk[: self._limit - self._kept]
                self._chunks.append(take)
                self._kept += len(take)

    @property
    def text(self) -> str:
        return b"".join(self._chunks).decode("utf-8", errors="replace")

    @property
    def truncated(self) -> bool:
        return self._total > self._limit


def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
    with suppress(ProcessLookupError):
        proc.kill()


async def _run_exec(cmd: list[str], timeout: int = 30) -> tuple[bytes, bytes, int] | dict:
    """Run an argv command (no shell). Returns (stdout, stderr, exit_code) or an error dict."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return {"error": f"{cmd[0]} not found on host"}
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return {"error": f"{' '.join(cmd[:2])} timed out"}
    return stdout, stderr, proc.returncode or 0


def _walk_files(root: Path) -> Iterator[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            yield Path(dirpath) / name


def _audit(tool: str, **fields: object) -> None:
    logger.info("%s %s", tool, " ".join(f"{k}={v!r}" for k, v in fields.items()))


def _deny_check(command: str) -> str | None:
    for pattern in DENY_PATTERNS:
        if pattern.search(command):
            return f"command matches denylist pattern: {pattern.pattern}"
    return None


@dataclass
class _ShellProc:
    """A spawned shell command with bounded output capture.

    The process runs in its own session/process group so kills reach its
    children, and stdout/stderr are drained concurrently into capped buffers
    so the child can never block on a full pipe.
    """

    proc: asyncio.subprocess.Process
    stdout: _StreamBuffer
    stderr: _StreamBuffer
    tasks: set[asyncio.Task] = field(default_factory=set)

    async def finish_io(self) -> None:
        """Wait for the drain tasks after the process has exited.

        Pipes hit EOF once the group is dead; the cutoff guards against a
        grandchild that detached into its own session and kept them open.
        """
        _, pending = await asyncio.wait(self.tasks, timeout=5)
        for task in pending:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


async def _spawn_shell(
    command: str,
    cwd_path: Path,
    env: dict[str, str] | None,
    stdin: str | None,
) -> _ShellProc:
    proc_env = os.environ.copy()
    if env:
        proc_env.update(env)

    proc = await asyncio.create_subprocess_shell(
        command,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd_path),
        env=proc_env,
        start_new_session=True,  # own process group, so kills reach children too
    )

    async def feed_stdin() -> None:
        if stdin is None or proc.stdin is None:
            return
        with suppress(BrokenPipeError, ConnectionResetError):
            proc.stdin.write(stdin.encode("utf-8"))
            await proc.stdin.drain()
        proc.stdin.close()

    sp = _ShellProc(proc, _StreamBuffer(MAX_OUTPUT_BYTES), _StreamBuffer(MAX_OUTPUT_BYTES))
    sp.tasks.add(asyncio.create_task(feed_stdin()))
    sp.tasks.add(asyncio.create_task(sp.stdout.drain(proc.stdout)))
    sp.tasks.add(asyncio.create_task(sp.stderr.drain(proc.stderr)))
    return sp


# --- Tools ---
@mcp.tool()
async def run_command(
    command: str,
    cwd: str = DEFAULT_CWD,
    timeout_seconds: int = 30,
    env: dict[str, str] | None = None,
    stdin: str | None = None,
) -> dict:
    """Execute a shell command on the host and wait for it to finish.

    For work that outlives the 120s cap (builds, migrations, servers),
    use job_start instead.

    Args:
        command: The shell command to run.
        cwd: Working directory. Defaults to GRAPPA_MCP_DEFAULT_CWD or $HOME.
        timeout_seconds: Max execution time, capped at 120s. On timeout the
            whole process group is killed and partial output is returned.
        env: Optional environment variable overrides.
        stdin: Optional text piped to the command's standard input.
    """
    if error := _deny_check(command):
        return {"error": error}

    timeout_seconds = max(1, min(timeout_seconds, MAX_TIMEOUT))
    cwd_path = _normalize_path(cwd)
    if not cwd_path.is_dir():
        return {"error": f"cwd does not exist: {cwd_path}"}

    _audit("run_command", cwd=str(cwd_path), timeout=timeout_seconds, command=command)
    sp = await _spawn_shell(command, cwd_path, env, stdin)

    timed_out = False
    try:
        await asyncio.wait_for(sp.proc.wait(), timeout=timeout_seconds)
    except TimeoutError:
        timed_out = True
        _kill_process_group(sp.proc)
        await sp.proc.wait()

    await sp.finish_io()

    return {
        "exit_code": sp.proc.returncode if not timed_out else -1,
        "stdout": sp.stdout.text,
        "stderr": sp.stderr.text,
        "timed_out": timed_out,
        "stdout_truncated": sp.stdout.truncated,
        "stderr_truncated": sp.stderr.truncated,
    }


# --- Background jobs ---
# In-memory registry: jobs do not survive a server restart. Finished jobs are
# kept (with their captured output) until evicted by MAX_FINISHED_JOBS.
@dataclass
class _Job:
    id: str
    command: str
    cwd: str
    sp: _ShellProc
    started_at: datetime
    done: asyncio.Event = field(default_factory=asyncio.Event)
    finished_at: datetime | None = None
    killed: bool = False
    monitor: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self.sp.proc.returncode is None


_JOBS: dict[str, _Job] = {}


def _job_summary(job: _Job) -> dict:
    end = job.finished_at or datetime.now(UTC)
    if job.running:
        state = "running"
    else:
        state = "killed" if job.killed else "exited"
    return {
        "job_id": job.id,
        "command": job.command,
        "cwd": job.cwd,
        "pid": job.sp.proc.pid,
        "state": state,
        "exit_code": job.sp.proc.returncode,
        "started_at": job.started_at.isoformat(),
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "runtime_seconds": round((end - job.started_at).total_seconds(), 3),
    }


def _evict_finished_jobs() -> None:
    finished = sorted(
        (j for j in _JOBS.values() if not j.running and j.finished_at),
        key=lambda j: j.finished_at,
    )
    for job in finished[: max(0, len(finished) - MAX_FINISHED_JOBS)]:
        del _JOBS[job.id]


@mcp.tool()
async def job_start(
    command: str,
    cwd: str = DEFAULT_CWD,
    env: dict[str, str] | None = None,
    stdin: str | None = None,
) -> dict:
    """Start a shell command as a background job on the host.

    Use this for work that outlives run_command's 120s cap (builds,
    migrations, servers under test). The job keeps running between calls;
    poll it with job_status and stop it with job_kill. Jobs live in server
    memory and do not survive a server restart.

    Args:
        command: The shell command to run.
        cwd: Working directory. Defaults to GRAPPA_MCP_DEFAULT_CWD or $HOME.
        env: Optional environment variable overrides.
        stdin: Optional text piped to the command's standard input.
    """
    if error := _deny_check(command):
        return {"error": error}

    cwd_path = _normalize_path(cwd)
    if not cwd_path.is_dir():
        return {"error": f"cwd does not exist: {cwd_path}"}

    running = sum(1 for j in _JOBS.values() if j.running)
    if running >= MAX_RUNNING_JOBS:
        return {"error": f"too many running jobs ({running}); kill or wait for some first"}

    _audit("job_start", cwd=str(cwd_path), command=command)
    sp = await _spawn_shell(command, cwd_path, env, stdin)
    job = _Job(
        id=f"job-{secrets.token_hex(4)}",
        command=command,
        cwd=str(cwd_path),
        sp=sp,
        started_at=datetime.now(UTC),
    )

    async def monitor() -> None:
        await sp.proc.wait()
        await sp.finish_io()
        job.finished_at = datetime.now(UTC)
        job.done.set()
        _audit("job_finished", job_id=job.id, exit_code=sp.proc.returncode)

    job.monitor = asyncio.create_task(monitor())
    _JOBS[job.id] = job
    _evict_finished_jobs()

    return {"job_id": job.id, "pid": sp.proc.pid, "state": "running"}


@mcp.tool()
async def job_status(job_id: str, wait_seconds: int = 0) -> dict:
    """Get a background job's state and the output captured so far.

    Args:
        job_id: The id returned by job_start.
        wait_seconds: If > 0, block up to this many seconds (capped at 60)
            for the job to finish before reporting. Lets a poll loop sleep
            server-side instead of hammering the tool.
    """
    job = _JOBS.get(job_id)
    if job is None:
        return {"error": f"unknown job: {job_id} (jobs are lost on server restart)"}

    wait_seconds = max(0, min(wait_seconds, MAX_JOB_WAIT))
    if wait_seconds and not job.done.is_set():
        with suppress(TimeoutError):
            await asyncio.wait_for(job.done.wait(), timeout=wait_seconds)

    return _job_summary(job) | {
        "stdout": job.sp.stdout.text,
        "stderr": job.sp.stderr.text,
        "stdout_truncated": job.sp.stdout.truncated,
        "stderr_truncated": job.sp.stderr.truncated,
    }


@mcp.tool()
async def job_kill(job_id: str) -> dict:
    """Kill a running background job (SIGKILL to its whole process group).

    Args:
        job_id: The id returned by job_start.
    """
    job = _JOBS.get(job_id)
    if job is None:
        return {"error": f"unknown job: {job_id} (jobs are lost on server restart)"}
    if not job.running:
        return {"error": "job already finished"} | _job_summary(job)

    _audit("job_kill", job_id=job.id, command=job.command)
    job.killed = True
    _kill_process_group(job.sp.proc)
    with suppress(TimeoutError):
        await asyncio.wait_for(job.done.wait(), timeout=5)

    return _job_summary(job)


@mcp.tool()
async def job_list() -> dict:
    """List background jobs (running, and finished ones not yet evicted)."""
    jobs = sorted(_JOBS.values(), key=lambda j: j.started_at)
    return {"jobs": [_job_summary(j) for j in jobs]}


@mcp.tool()
async def read_file(
    path: str,
    offset: int = 1,
    limit: int | None = None,
) -> dict:
    """Read a text file on the host.

    Args:
        path: Absolute or relative file path.
        offset: 1-based line number to start reading from.
        limit: Maximum number of lines to return (whole file if unset).
    """
    p = _normalize_path(path)
    if not p.is_file():
        return {"error": f"file not found: {p}"}

    try:
        size = p.stat().st_size
        if size > MAX_READ_BYTES:
            return {
                "error": f"file too large ({size} bytes, max {MAX_READ_BYTES}); "
                "use run_command to slice it"
            }
        raw = p.read_bytes()
    except OSError as e:
        return {"error": str(e)}

    lines = raw.decode("utf-8", errors="replace").splitlines(keepends=True)
    total_lines = len(lines)
    start = max(offset, 1) - 1
    selected = lines[start:] if limit is None else lines[start : start + limit]

    return {
        "content": "".join(selected),
        "offset": start + 1,
        "lines_returned": len(selected),
        "total_lines": total_lines,
        "truncated": start + len(selected) < total_lines,
        "sha256": _sha256(raw),
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

    _audit("write_file", path=str(p), bytes=len(content.encode("utf-8")), overwrite=overwrite)
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
        expected_sha256: Optional SHA-256 hash of the file before editing;
            fails on mismatch. read_file returns the current hash.
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
        raw = p.read_bytes()
    except OSError as e:
        return {"error": str(e)}

    # Hash the bytes we actually read, so the check cannot race a concurrent write.
    if expected_sha256:
        actual_hash = _sha256(raw)
        if not hmac.compare_digest(actual_hash, expected_sha256):
            return {
                "error": "sha256 mismatch — file was modified",
                "actual_sha256": actual_hash,
            }

    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        return {"error": f"file is not valid UTF-8: {e}"}

    count = content.count(old_text)
    if count == 0:
        return {"error": "old_text not found in file"}
    if count > 1 and not replace_all:
        return {"error": f"ambiguous: old_text found {count} times (set replace_all=true)"}

    new_content = content.replace(old_text, new_text, -1 if replace_all else 1)

    _audit("edit_file", path=str(p), replacements=count if replace_all else 1)
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
    except (OSError, ValueError) as e:
        return {"error": str(e)}

    max_entries = 2000
    entries.sort()
    truncated = len(entries) > max_entries
    entries = entries[:max_entries]

    result = []
    for entry in entries:
        try:
            st = entry.stat(follow_symlinks=False)
        except OSError:
            continue  # entry vanished mid-listing
        if statmod.S_ISLNK(st.st_mode):
            etype = "symlink"
        elif statmod.S_ISDIR(st.st_mode):
            etype = "dir"
        else:
            etype = "file"
        result.append(
            {
                "name": str(entry.relative_to(p)),
                "type": etype,
                "size": st.st_size if etype == "file" else None,
            }
        )

    return {"path": str(p), "entries": result, "truncated": truncated}


@mcp.tool()
async def search_files(
    pattern: str,
    path: str = DEFAULT_CWD,
    glob: str | None = None,
    case_sensitive: bool = True,
    max_results: int = 200,
) -> dict:
    """Search file contents with a regular expression, like grep -rn.

    Skips binary files, files over 2 MB, and vendor/VCS directories
    (.git, node_modules, virtualenvs, caches).

    Args:
        pattern: Python regular expression to search for.
        path: Directory (or single file) to search. Recurses by default.
        glob: Optional filename filter, e.g. '*.py'.
        case_sensitive: If false, match case-insensitively.
        max_results: Maximum number of matching lines to return (capped at 1000).
    """
    try:
        regex = re.compile(pattern, 0 if case_sensitive else re.IGNORECASE)
    except re.error as e:
        return {"error": f"invalid regex: {e}"}

    root = _normalize_path(path)
    if root.is_file():
        candidates: Iterator[Path] = iter([root])
    elif root.is_dir():
        candidates = _walk_files(root)
    else:
        return {"error": f"path not found: {root}"}

    max_results = max(1, min(max_results, 1000))
    matches: list[dict] = []
    files_scanned = 0
    truncated = False
    for f in candidates:
        if glob and not fnmatch.fnmatch(f.name, glob):
            continue
        try:
            if f.stat().st_size > MAX_SEARCH_FILE_BYTES:
                continue
            raw = f.read_bytes()
        except OSError:
            continue
        if b"\0" in raw[:8192]:
            continue  # binary
        files_scanned += 1
        for lineno, line in enumerate(raw.decode("utf-8", errors="replace").splitlines(), 1):
            if regex.search(line):
                matches.append({"file": str(f), "line": lineno, "text": line[:500]})
                if len(matches) >= max_results:
                    truncated = True
                    break
        if truncated:
            break

    return {
        "path": str(root),
        "matches": matches,
        "files_scanned": files_scanned,
        "truncated": truncated,
    }


@mcp.tool()
async def file_stat(path: str) -> dict:
    """Return metadata for a path on the host, including SHA-256 for regular files.

    Args:
        path: Absolute or relative path.
    """
    p = _normalize_path_keep_link(path)
    try:
        st = p.stat(follow_symlinks=False)
    except FileNotFoundError:
        return {"path": str(p), "exists": False}
    except OSError as e:
        return {"error": str(e)}

    if statmod.S_ISLNK(st.st_mode):
        etype = "symlink"
    elif statmod.S_ISDIR(st.st_mode):
        etype = "dir"
    else:
        etype = "file"

    out = {
        "path": str(p),
        "exists": True,
        "type": etype,
        "size": st.st_size,
        "mode": f"{statmod.S_IMODE(st.st_mode):04o}",
        "mtime": datetime.fromtimestamp(st.st_mtime, tz=UTC).isoformat(),
    }
    if etype == "file":
        with suppress(OSError):
            out["sha256"] = _file_hash(p)
    return out


@mcp.tool()
async def delete_file(path: str, recursive: bool = False) -> dict:
    """Delete a file, symlink, or directory on the host.

    Args:
        path: Absolute or relative path.
        recursive: Required to delete a non-empty directory.
    """
    p = _normalize_path_keep_link(path)
    protected = {Path("/"), Path.home(), Path(DEFAULT_CWD)}
    if p in protected:
        return {"error": f"refusing to delete protected path: {p}"}
    try:
        st = p.stat(follow_symlinks=False)
    except FileNotFoundError:
        return {"error": f"path not found: {p}"}
    except OSError as e:
        return {"error": str(e)}

    _audit("delete_file", path=str(p), recursive=recursive)
    try:
        if statmod.S_ISDIR(st.st_mode):
            if recursive:
                shutil.rmtree(p)
            else:
                p.rmdir()
        else:
            p.unlink()
    except OSError as e:
        if e.errno == errno.ENOTEMPTY:
            return {"error": f"directory not empty (set recursive=true): {p}"}
        return {"error": str(e)}

    return {"path": str(p), "deleted": True}


@mcp.tool()
async def move_file(src: str, dst: str, overwrite: bool = False) -> dict:
    """Move or rename a file or directory on the host.

    Args:
        src: Source path.
        dst: Destination path. If it is an existing directory, the source is
            moved into it (like mv).
        overwrite: If false, refuse to replace an existing destination file.
    """
    s = _normalize_path_keep_link(src)
    if not s.exists() and not s.is_symlink():
        return {"error": f"source not found: {s}"}
    d = _normalize_path(dst)
    if d.is_dir():
        d = d / s.name
    if (d.exists() or d.is_symlink()) and not overwrite:
        return {"error": f"destination already exists (set overwrite=true): {d}"}

    _audit("move_file", src=str(s), dst=str(d))
    try:
        shutil.move(str(s), str(d))
    except OSError as e:
        return {"error": str(e)}

    return {"src": str(s), "dst": str(d)}


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
    if not _CONTAINER_NAME_RE.fullmatch(container):
        return {"error": f"invalid container name: {container}"}

    tail = max(1, min(tail, 100_000))
    cmd = ["docker", "logs", "--tail", str(tail)]
    if since:
        cmd.extend(["--since", since])
    cmd.append(container)

    _audit("docker_logs", container=container, tail=tail, since=since)
    res = await _run_exec(cmd)
    if isinstance(res, dict):
        return res
    stdout, stderr, _exit_code = res

    stdout_text = stdout[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
    stderr_text = stderr[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")

    return {
        "container": container,
        "stdout": stdout_text,
        "stderr": stderr_text,
        "stdout_truncated": len(stdout) > MAX_OUTPUT_BYTES,
        "stderr_truncated": len(stderr) > MAX_OUTPUT_BYTES,
    }


@mcp.tool()
async def docker_ps(all: bool = False) -> dict:
    """List Docker containers on the host.

    Args:
        all: If true, include stopped containers.
    """
    cmd = ["docker", "ps", "--no-trunc", "--format", "{{json .}}"]
    if all:
        cmd.append("--all")

    res = await _run_exec(cmd)
    if isinstance(res, dict):
        return res
    stdout, stderr, exit_code = res
    if exit_code != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()
        return {"error": detail or f"docker ps exited {exit_code}"}

    containers = []
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        if line.strip():
            with suppress(json.JSONDecodeError):
                containers.append(json.loads(line))
    return {"containers": containers}


@mcp.tool()
async def docker_restart(
    container: str | None = None,
    stop_timeout: int = 10,
) -> dict:
    """Restart a Docker container on the host.

    Args:
        container: Container name. Falls back to GRAPPA_MCP_DEFAULT_DOCKER_CONTAINER.
        stop_timeout: Seconds to wait for a graceful stop before the daemon
            kills the container (docker restart --time).
    """
    container = container or DEFAULT_DOCKER_CONTAINER
    if not container:
        return {"error": "container is required (or set GRAPPA_MCP_DEFAULT_DOCKER_CONTAINER)"}
    if not _CONTAINER_NAME_RE.fullmatch(container):
        return {"error": f"invalid container name: {container}"}

    stop_timeout = max(0, min(stop_timeout, 300))
    _audit("docker_restart", container=container, stop_timeout=stop_timeout)
    res = await _run_exec(
        ["docker", "restart", "--time", str(stop_timeout), container],
        timeout=stop_timeout + 60,
    )
    if isinstance(res, dict):
        return res
    _stdout, stderr, exit_code = res
    if exit_code != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()
        return {"error": detail or f"docker restart exited {exit_code}"}

    return {"container": container, "restarted": True}


# --- App setup ---
@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(request: Request) -> JSONResponse:
    """Unauthenticated liveness probe for uptime monitors."""
    return JSONResponse({"status": "ok"})


def create_app() -> ASGIApp:
    """Build the ASGI app: MCP over HTTP behind bearer-token auth."""
    tokens = _load_tokens()
    if not tokens:
        raise RuntimeError("GRAPPA_MCP_TOKEN not set")
    logging.basicConfig(
        level=LOG_LEVEL,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    return BearerAuthMiddleware(mcp.http_app(), tokens)


app = create_app()
