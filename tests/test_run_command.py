import subprocess
import time

import pytest

import server


async def test_basic_stdout(call):
    result = await call("run_command", command="echo hello")
    assert result["exit_code"] == 0
    assert result["stdout"] == "hello\n"
    assert result["stderr"] == ""
    assert result["timed_out"] is False


async def test_exit_code_and_stderr(call):
    result = await call("run_command", command="echo oops >&2; exit 3")
    assert result["exit_code"] == 3
    assert result["stderr"] == "oops\n"


async def test_cwd(call, tmp_path):
    result = await call("run_command", command="pwd", cwd=str(tmp_path))
    assert result["stdout"].strip() == str(tmp_path)


async def test_cwd_tilde_expansion(call):
    result = await call("run_command", command="pwd", cwd="~")
    assert result["exit_code"] == 0


async def test_cwd_missing(call):
    result = await call("run_command", command="true", cwd="/definitely/not/here")
    assert "cwd does not exist" in result["error"]


async def test_env_override(call):
    result = await call(
        "run_command", command="echo $GRAPPA_TEST_VAR", env={"GRAPPA_TEST_VAR": "abc"}
    )
    assert result["stdout"] == "abc\n"


async def test_stdin(call):
    result = await call("run_command", command="cat", stdin="hello stdin")
    assert result["stdout"] == "hello stdin"


async def test_timeout_reports_and_returns_partial_output(call):
    start = time.monotonic()
    result = await call(
        "run_command",
        command="echo before; sleep 30; echo after",
        timeout_seconds=1,
    )
    assert time.monotonic() - start < 10
    assert result["timed_out"] is True
    assert result["exit_code"] == -1
    assert "before" in result["stdout"]
    assert "after" not in result["stdout"]


async def test_timeout_kills_process_group(call):
    marker = "sleep 987.654"
    result = await call(
        "run_command",
        command=f"{marker} & {marker}",
        timeout_seconds=1,
    )
    assert result["timed_out"] is True
    # The backgrounded child must not survive the group kill.
    leftover = subprocess.run(["pgrep", "-f", marker], capture_output=True)
    assert leftover.returncode != 0, f"orphaned children: {leftover.stdout}"


async def test_timeout_clamped_to_at_least_one_second(call):
    result = await call("run_command", command="echo ok", timeout_seconds=0)
    assert result["exit_code"] == 0
    assert result["timed_out"] is False


async def test_output_truncated_at_cap(call):
    n = server.MAX_OUTPUT_BYTES + 50_000
    result = await call("run_command", command=f"yes a | head -c {n}")
    assert result["stdout_truncated"] is True
    assert len(result["stdout"]) == server.MAX_OUTPUT_BYTES


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf /",
        "rm -rf /*",
        "rm -fr /",
        "rm --recursive --force /",
        "rm --no-preserve-root -rf /",
        "rm -rf / --no-preserve-root",
        "echo hi && rm -rf /",
        "mkfs.ext4 /dev/sda1",
        "mkfs /dev/sda",
        "dd if=/dev/zero of=/dev/sda",
        "echo x > /dev/sda",
    ],
)
async def test_denylist_blocks(call, command):
    result = await call("run_command", command=command)
    assert "denylist" in result["error"]


@pytest.mark.parametrize(
    "command",
    [
        "echo rm -rf /tmp/build is fine",
        "ls /",
        "ddate",
        "echo mkfsy",
        "dd if=/dev/zero of=/tmp/testfile bs=1 count=1",
    ],
)
async def test_denylist_allows_benign_commands(call, command):
    result = await call("run_command", command=command)
    assert "error" not in result


async def test_denylist_allows_rm_rf_with_real_path(call, tmp_path):
    victim = tmp_path / "build"
    victim.mkdir()
    (victim / "artifact.txt").write_text("x")
    result = await call("run_command", command=f"rm -rf {victim}")
    assert result.get("exit_code") == 0, result
    assert not victim.exists()
