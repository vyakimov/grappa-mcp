async def test_docker_logs_requires_container(call, monkeypatch):
    import server

    monkeypatch.setattr(server, "DEFAULT_DOCKER_CONTAINER", None)
    result = await call("docker_logs")
    assert "container is required" in result["error"]


async def test_docker_logs_rejects_flag_like_container_name(call):
    result = await call("docker_logs", container="--help")
    assert "invalid container name" in result["error"]


async def test_docker_missing_binary_is_clean_error(call, monkeypatch):
    # Make the docker binary unfindable to exercise the FileNotFoundError path.
    monkeypatch.setenv("PATH", "/nonexistent")
    result = await call("docker_logs", container="myapp")
    assert result["error"] == "docker not found on host"
