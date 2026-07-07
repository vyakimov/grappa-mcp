import os

# Must be set before `server` is imported anywhere: create_app() runs at
# import time and refuses to start without a token.
os.environ.setdefault("GRAPPA_MCP_TOKEN", "test-token")

import pytest
from fastmcp import Client

import server


@pytest.fixture
async def client():
    async with Client(server.mcp) as c:
        yield c


@pytest.fixture
def call(client):
    """Call a tool and return its structured result dict."""

    async def _call(tool: str, **args):
        result = await client.call_tool(tool, args)
        return result.data

    return _call
