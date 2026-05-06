"""End-to-end test of the MCP server: spawn it as a subprocess, talk via stdio."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_CC = REPO_ROOT / "data" / "fixture" / "cc-input.jsonl"


@pytest.fixture
def server_cmd() -> list[str]:
    return [sys.executable, "-m", "agent_bridge.cli", "mcp", "serve"]


@pytest.mark.asyncio
async def test_mcp_lists_tools_and_calls_sync_now(server_cmd, tmp_path) -> None:
    """Smoke: spawn server, list tools, invoke a tool, assert response."""
    pytest.importorskip("mcp")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=server_cmd[0],
        args=server_cmd[1:],
        env={**os.environ},
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()
            tool_names = {t.name for t in result.tools}
            assert "list_sessions" in tool_names
            assert "translate_session" in tool_names
            assert "sync_now" in tool_names
            assert "find_session" in tool_names
            assert "prepare_resume" in tool_names
            assert "resume_with_prompt" in tool_names

            # sync_now is safe to call (idempotent; max_bytes can be 0 to skip everything)
            sync_result = await session.call_tool(
                "sync_now",
                {"direction": "both", "days": 0, "max_bytes": 1},
            )
            # It returns at least the stats dict in the content
            assert sync_result.content
            text = sync_result.content[0].text
            data = json.loads(text)
            assert "translated" in data
            assert "skipped_existing" in data


@pytest.mark.asyncio
async def test_mcp_list_sessions_returns_array(server_cmd) -> None:
    pytest.importorskip("mcp")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=server_cmd[0],
        args=server_cmd[1:],
        env={**os.environ},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            res = await session.call_tool(
                "list_sessions",
                {"harness": "claude-code", "days": 1, "limit": 3},
            )
            assert res.content
            data = json.loads(res.content[0].text)
            assert isinstance(data, dict)
            assert data["harness"] == "claude-code"
            assert isinstance(data["sessions"], list)
            for item in data["sessions"]:
                assert "session_id" in item
                assert "harness" in item
                assert item["harness"] == "claude-code"
