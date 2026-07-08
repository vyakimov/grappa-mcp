import subprocess

import pytest

import server


@pytest.fixture(autouse=True)
def clean_registry():
    server._JOBS.clear()
    yield
    for job in server._JOBS.values():
        if job.running:
            server._kill_process_group(job.sp.proc)
    server._JOBS.clear()


async def test_job_lifecycle(call):
    started = await call("job_start", command="echo working; exit 7")
    assert started["state"] == "running"
    job_id = started["job_id"]

    result = await call("job_status", job_id=job_id, wait_seconds=10)
    assert result["state"] == "exited"
    assert result["exit_code"] == 7
    assert result["stdout"] == "working\n"
    assert result["finished_at"] is not None
    assert result["runtime_seconds"] >= 0


async def test_job_streams_partial_output_while_running(call):
    started = await call("job_start", command="echo early; sleep 30; echo late")
    result = await call("job_status", job_id=started["job_id"], wait_seconds=2)
    assert result["state"] == "running"
    assert "early" in result["stdout"]
    assert "late" not in result["stdout"]

    killed = await call("job_kill", job_id=started["job_id"])
    assert killed["state"] == "killed"


async def test_job_kill_terminates_process_group(call):
    marker = "sleep 876.543"
    started = await call("job_start", command=f"{marker} & {marker}")
    result = await call("job_kill", job_id=started["job_id"])
    assert result["state"] == "killed"
    leftover = subprocess.run(["pgrep", "-f", marker], capture_output=True)
    assert leftover.returncode != 0, f"orphaned children: {leftover.stdout}"


async def test_job_kill_finished_job_errors(call):
    started = await call("job_start", command="true")
    await call("job_status", job_id=started["job_id"], wait_seconds=10)
    result = await call("job_kill", job_id=started["job_id"])
    assert "already finished" in result["error"]


async def test_unknown_job_id(call):
    for tool in ("job_status", "job_kill"):
        result = await call(tool, job_id="job-deadbeef")
        assert "unknown job" in result["error"]


async def test_job_start_respects_denylist(call):
    result = await call("job_start", command="rm -rf /")
    assert "denylist" in result["error"]


async def test_job_start_stdin_and_env(call):
    started = await call(
        "job_start", command="cat; echo $GRAPPA_JOB_VAR",
        stdin="piped", env={"GRAPPA_JOB_VAR": "vv"},
    )
    result = await call("job_status", job_id=started["job_id"], wait_seconds=10)
    assert result["stdout"] == "pipedvv\n"


async def test_job_list(call):
    a = await call("job_start", command="sleep 30")
    b = await call("job_start", command="true")
    await call("job_status", job_id=b["job_id"], wait_seconds=10)

    result = await call("job_list")
    by_id = {j["job_id"]: j for j in result["jobs"]}
    assert by_id[a["job_id"]]["state"] == "running"
    assert by_id[b["job_id"]]["state"] == "exited"
    # summaries must not carry output payloads
    assert "stdout" not in by_id[a["job_id"]]

    await call("job_kill", job_id=a["job_id"])


async def test_running_job_limit(call, monkeypatch):
    monkeypatch.setattr(server, "MAX_RUNNING_JOBS", 1)
    a = await call("job_start", command="sleep 30")
    result = await call("job_start", command="true")
    assert "too many running jobs" in result["error"]
    await call("job_kill", job_id=a["job_id"])


async def test_finished_job_eviction(call, monkeypatch):
    monkeypatch.setattr(server, "MAX_FINISHED_JOBS", 1)
    first = await call("job_start", command="true")
    await call("job_status", job_id=first["job_id"], wait_seconds=10)
    second = await call("job_start", command="true")
    await call("job_status", job_id=second["job_id"], wait_seconds=10)
    # Starting a third prunes the oldest finished job beyond the cap.
    third = await call("job_start", command="true")
    await call("job_status", job_id=third["job_id"], wait_seconds=10)

    result = await call("job_status", job_id=first["job_id"])
    assert "unknown job" in result["error"]
    assert "error" not in await call("job_status", job_id=second["job_id"])
