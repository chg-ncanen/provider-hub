"""MCP application wiring for the PDE MCP server."""

# The plugin README documents Python 3.9+ as a prerequisite, but this file uses
# PEP 604 `X | Y` union syntax (e.g. `AppConfig | None`), which only evaluates
# at runtime on 3.10+ — deferring annotation evaluation keeps it working on 3.9.
from __future__ import annotations

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

# Copilot CLI loads the same .mcp.json as Claude Code but has no `userConfig`
# of its own, so it doesn't understand .mcp.json's ${user_config.*} env
# substitution — verified against the real `copilot` CLI that it passes the
# literal, unsubstituted string through as the credential's value instead of
# leaving it unset. Strip that placeholder before load_dotenv() runs, or its
# default override=False would treat the credential as "already set" and
# silently ignore a hand-written .env, breaking the documented Copilot CLI
# fallback (copy .env.example to .env).
_USERCONFIG_PLACEHOLDER_PREFIX = "${user_config."
for _credential_env_var in ("ATLASSIAN_EMAIL", "ATLASSIAN_API_TOKEN", "EMAIL_USERNAME", "EMAIL_PASSWORD"):
    if os.environ.get(_credential_env_var, "").startswith(_USERCONFIG_PLACEHOLDER_PREFIX):
        del os.environ[_credential_env_var]

try:
    from dotenv import load_dotenv

    # Claude Code passes userConfig credentials straight into this process's
    # environment via .mcp.json's ${user_config.*} substitution, so this is
    # only load-bearing for Copilot CLI (no userConfig equivalent) or local
    # dev, where a user creates this .env by hand from .env.example.
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
