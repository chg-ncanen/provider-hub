"""MCP application wiring for the PDE JSM server."""

import asyncio
import os
from pathlib import Path
import sys
from threading import Lock
from typing import Any

MCP_DIR = Path(__file__).resolve().parent
# CLAUDE_PLUGIN_ROOT is set by Claude Code when this runs as a plugin; fall back
# to the on-disk plugin root (two levels up: mcp-servers/pde-mcp -> plugins/pde)
# for local/manual runs.
PLUGIN_ROOT = Path(os.environ.get("CLAUDE_PLUGIN_ROOT", str(MCP_DIR.parents[1])))

if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import mcp.server.stdio
import mcp.types as types
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions

from api.jsm.client import JSMOpsAPI
from api.jsm.config import AppConfig
from api.mail.email_tool import EmailTool
from tools import alerts, email, skills

try:
    from dotenv import load_dotenv

    # The SessionStart hook (bootstrap-venv.sh) writes credentials here when
    # available; otherwise this is whatever a user set up by hand.
    load_dotenv(MCP_DIR / ".env")
except Exception:
    pass

server = Server("pde-mcp")

_cfg: AppConfig | None = None
_api: JSMOpsAPI | None = None
_email: EmailTool | None = None
_lock = Lock()


def _get_api() -> JSMOpsAPI:
    global _cfg, _api
    with _lock:
        if _api is None:
            _cfg = AppConfig.from_env(config_path=MCP_DIR / "app_config.json")
            _api = JSMOpsAPI(config=_cfg)
    return _api


def _get_email() -> EmailTool:
    global _email
    with _lock:
        if _email is None:
            _email = EmailTool()
    return _email


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return alerts.definitions() + email.definitions() + skills.definitions()


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
    if alerts.can_handle(name):
        payload = alerts.handle(name=name, arguments=arguments, api=_get_api())
        return alerts.as_text_content(payload)

    if email.can_handle(name):
        payload = email.handle(name=name, arguments=arguments, email_tool=_get_email())
        return email.as_text_content(payload)

    if skills.can_handle(name):
        payload = skills.handle(name=name, arguments=arguments, project_root=PLUGIN_ROOT)
        return skills.as_text_content(payload)

    raise ValueError(f"Unknown tool: {name}")


async def main() -> None:
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="pde-mcp",
                server_version="1.0.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())
