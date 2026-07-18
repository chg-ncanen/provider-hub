"""Detect which client/environment is running this setup skill."""

import os
import platform
from pathlib import Path
from enum import Enum


class Environment(Enum):
    """Supported environments for provider-hub setup."""
    COPILOT_CLI = "copilot-cli"          # Copilot CLI (GitHub)
    CLAUDE_CODE_CLI = "claude-code-cli"  # Claude Code CLI (Anthropic)
    CLAUDE_DESKTOP = "claude-desktop"    # Claude Desktop (Anthropic)
    COPILOT_DESKTOP = "copilot-desktop"  # GitHub Copilot Desktop
    VSCODE_COPILOT = "vscode-copilot"    # VS Code + GitHub Copilot
    VSCODE_CLAUDE = "vscode-claude"      # VS Code + Claude extension
    UNKNOWN = "unknown"


def _claude_code_config_file() -> Path:
    """Claude Code CLI stores all config (incl. per-project MCP servers) here."""
    return Path.home() / ".claude.json"


def _claude_desktop_config_file() -> Path:
    """Claude Desktop's config path differs per OS; it is never under ~/.claude/."""
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if system == "Windows":
        appdata = os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))
        return Path(appdata) / "Claude" / "claude_desktop_config.json"
    # Linux (unofficial builds commonly land here)
    return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


def detect_environment() -> tuple[Environment, dict]:
    """
    Detect which client is running this setup script.

    Returns:
        (Environment, metadata_dict) where metadata has client-specific info
        like config paths, CLI names, etc.
    """

    env_vars = os.environ

    # Claude Code CLI sets CLAUDECODE=1 and CLAUDE_CODE_SESSION_ID; check this
    # before the legacy/manual signals below since it's the most reliable.
    if env_vars.get("CLAUDECODE") == "1" or "CLAUDE_CODE_SESSION_ID" in env_vars:
        return Environment.CLAUDE_CODE_CLI, {
            "config_file": _claude_code_config_file(),
            "cli_name": "claude",
        }

    # Copilot CLI sets specific vars
    if "COPILOT_CLI" in env_vars or env_vars.get("COPILOT_VERSION"):
        return Environment.COPILOT_CLI, {
            "mcp_config": Path.cwd() / ".copilot-config.json",
            "cli_name": "copilot",
        }

    # Legacy/manual Claude CLI signal (e.g. run outside a Claude Code session
    # but with an API key configured)
    if "CLAUDE_CLI" in env_vars or env_vars.get("ANTHROPIC_API_KEY"):
        return Environment.CLAUDE_CODE_CLI, {
            "config_file": _claude_code_config_file(),
            "cli_name": "claude",
        }

    # Check for VS Code
    if "VSCODE_PID" in env_vars or env_vars.get("TERM_PROGRAM") == "vscode":
        if "COPILOT" in env_vars.get("_", "").upper():
            return Environment.VSCODE_COPILOT, {
                "editor": "vscode",
                "extension": "copilot",
            }
        else:
            return Environment.VSCODE_CLAUDE, {
                "editor": "vscode",
                "extension": "claude",
                "config_file": _claude_code_config_file(),
            }

    # Check for Claude Desktop
    claude_desktop_config = _claude_desktop_config_file()
    if claude_desktop_config.exists():
        return Environment.CLAUDE_DESKTOP, {
            "config_file": claude_desktop_config,
        }

    # Check for Copilot Desktop (GitHub's desktop app)
    copilot_desktop_paths = [
        Path.home() / ".github" / "copilot" / "config.json",
        Path.home() / "AppData" / "Local" / "GitHub" / "Copilot" / "config.json",  # Windows
        Path.home() / "Library" / "Application Support" / "GitHub Copilot" / "config.json",  # macOS
    ]
    for path in copilot_desktop_paths:
        if path.exists():
            return Environment.COPILOT_DESKTOP, {
                "config_file": path,
                "config_dir": path.parent,
            }

    # Default
    return Environment.UNKNOWN, {}


def get_environment_info(env: Environment) -> dict:
    """Return setup instructions and config paths for detected environment."""

    info = {
        "environment": env.value,
        "setup_path": None,
        "mcp_registration": None,
        "notes": [],
    }

    if env == Environment.COPILOT_CLI:
        info["setup_path"] = ".copilot-config.json (in your project root)"
        info["mcp_registration"] = "Register MCPs in .copilot-config.json"
        info["notes"] = [
            "MCPs are registered per-project in .copilot-config.json",
            "Different projects can have different MCP sets",
        ]

    elif env == Environment.CLAUDE_CODE_CLI:
        info["setup_path"] = "~/.claude.json"
        info["mcp_registration"] = 'Add to projects["<project path>"].mcpServers in ~/.claude.json'
        info["notes"] = [
            "MCPs are scoped per-project under the 'projects' key",
            "Only this project's entry is affected; other projects are untouched",
        ]

    elif env == Environment.CLAUDE_DESKTOP:
        info["setup_path"] = str(_claude_desktop_config_file())
        info["mcp_registration"] = "Add to 'mcpServers' object in config"
        info["notes"] = [
            "Claude Desktop config is global, not per-project",
            "Changes require restarting Claude Desktop",
        ]

    elif env == Environment.COPILOT_DESKTOP:
        info["setup_path"] = "Copilot Desktop settings (limited MCP support)"
        info["mcp_registration"] = "Via settings UI or env vars"
        info["notes"] = [
            "Copilot Desktop has limited MCP support compared to CLI",
            "Check GitHub docs for current MCP availability",
        ]

    elif env == Environment.VSCODE_CLAUDE:
        info["setup_path"] = "~/.claude.json"
        info["mcp_registration"] = "Registered the same way as Claude Code CLI"
        info["notes"] = [
            "Claude extension in VS Code shares Claude Code CLI's ~/.claude.json",
        ]

    elif env == Environment.VSCODE_COPILOT:
        info["setup_path"] = "VS Code Copilot settings"
        info["mcp_registration"] = "Limited MCP support in VS Code Copilot"
        info["notes"] = [
            "VS Code Copilot has limited MCP integration",
            "Consider using Copilot CLI for full provider-hub support",
        ]

    else:
        info["setup_path"] = "Unknown environment"
        info["mcp_registration"] = "Unable to detect"
        info["notes"] = [
            "Could not identify which client is running this setup",
            "Try running from Copilot CLI, Claude Code CLI, or Claude Desktop",
        ]

    return info
