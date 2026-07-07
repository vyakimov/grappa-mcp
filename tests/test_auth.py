import httpx
import pytest

import server


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("GRAPPA_MCP_TOKEN", "tok-one, tok-two")
    return server.create_app()


@pytest.fixture
def http(app):
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_healthz_is_open(http):
    r = await http.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_missing_token_is_401(http):
    r = await http.get("/some/path")
    assert r.status_code == 401
    assert r.json() == {"error": "unauthorized"}


async def test_wrong_token_is_401(http):
    r = await http.get("/some/path", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


@pytest.mark.parametrize("token", ["tok-one", "tok-two"])
async def test_any_configured_token_passes(http, token):
    r = await http.get("/nonexistent", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 404  # authenticated, path just doesn't exist


async def test_raw_token_without_bearer_prefix_passes(http):
    r = await http.get("/nonexistent", headers={"Authorization": "tok-one"})
    assert r.status_code == 404


async def test_create_app_requires_token(monkeypatch):
    monkeypatch.setenv("GRAPPA_MCP_TOKEN", "")
    with pytest.raises(RuntimeError, match="GRAPPA_MCP_TOKEN"):
        server.create_app()
