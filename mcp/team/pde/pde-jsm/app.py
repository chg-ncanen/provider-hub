"""MCP application wiring for the PDE JSM server."""

import asyncio
from pathlib import Path
import sys
from threading import Lock
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MCP_DIR = Path(__file__).resolve().parent

# Avoid local top-level `mcp/` directory shadowing the external `mcp` package.
if str(PROJECT_ROOT) in sys.path:
    sys.path.remove(str(PROJECT_ROOT))

import mcp.server.stdio
import mcp.types as types
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

from api.jsm.client import JSMOpsAPI
from api.jsm.config import AppConfig
from api.email.email_tool import EmailTool
from tools import alerts, email, reporting, skills

try:
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
except Exception:
    pass

server = Server("pde-jsm")

_cfg: AppConfig | None = None
_api: JSMOpsAPI | None = None
_email: EmailTool | None = None
_lock = Lock()


def _get_api() -> JSMOpsAPI:
    global _cfg, _api
    with _lock:
        if _api is None:
            _cfg = AppConfig.from_env(config_path=PROJECT_ROOT / "app_config.json")
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
    return alerts.definitions() + reporting.definitions() + email.definitions() + skills.definitions()


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
    if alerts.can_handle(name):
        payload = alerts.handle(name=name, arguments=arguments, api=_get_api())
        return alerts.as_text_content(payload)

    if reporting.can_handle(name):
        payload = reporting.handle(name=name, arguments=arguments, project_root=PROJECT_ROOT)
        return reporting.as_text_content(payload)

    if email.can_handle(name):
        payload = email.handle(name=name, arguments=arguments, email_tool=_get_email())
        return email.as_text_content(payload)

    if skills.can_handle(name):
        payload = skills.handle(name=name, arguments=arguments, project_root=PROJECT_ROOT)
        return skills.as_text_content(payload)

    raise ValueError(f"Unknown tool: {name}")


async def main() -> None:
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="pde-jsm",
                server_version="1.0.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())
