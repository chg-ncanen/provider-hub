"""Detect which client/environment is running this setup skill."""

import os
import json
from pathlib import Path
from enum import Enum


class Environment(Enum):
    """Supported environments for provider-hub setup."""
    COPILOT_CLI = "copilot-cli"  # Copilot CLI (GitHub)
    CLAUDE_CLI = "claude-cli"    # Claude CLI (Anthropic)
    COPILOT_DESKTOP = "copilot-desktop"  # GitHub Copilot Desktop
    CLAUDE_DESKTOP = "claude-desktop"    # Claude Desktop (Anthropic)
    VSCODE_COPILOT = "vscode-copilot"    # VS Code + GitHub Copilot
    VSCODE_CLAUDE = "vscode-claude"      # VS Code + Claude extension
    UNKNOWN = "unknown"


def detect_environment() -> tuple[Environment, dict]:
    """
    Detect which client is running this setup script.
    
    Returns:
        (Environment, metadata_dict) where metadata has client-specific info
        like config paths, CLI names, etc.
    """
    
    # Check environment variables first (most reliable)
    env_vars = os.environ
    
    # Copilot CLI sets specific vars
    if "COPILOT_CLI" in env_vars or env_vars.get("COPILOT_VERSION"):
        return Environment.COPILOT_CLI, {
            "config_file": Path.home() / ".copilot" / "config.json",
            "mcp_config": Path.cwd() / ".copilot-config.json",
            "cli_name": "copilot",
        }
    
    # Claude CLI sets specific vars
    if "CLAUDE_CLI" in env_vars or env_vars.get("ANTHROPIC_API_KEY"):
        return Environment.CLAUDE_CLI, {
            "config_dir": Path.home() / ".claude",
            "cli_name": "claude",
        }
    
    # Check for VS Code
    if "VSCODE_PID" in env_vars or ("TERM_PROGRAM" in env_vars and env_vars["TERM_PROGRAM"] == "vscode"):
        # Distinguish between Copilot and Claude extensions
        if "COPILOT" in env_vars.get("_", "").upper():
            return Environment.VSCODE_COPILOT, {
                "editor": "vscode",
                "extension": "copilot",
            }
        else:
            return Environment.VSCODE_CLAUDE, {
                "editor": "vscode",
                "extension": "claude",
                "config_dir": Path.home() / ".claude",
            }
    
    # Check for Desktop apps by looking for config files
    claude_desktop_config = Path.home() / ".claude" / "claude_desktop_config.json"
    if claude_desktop_config.exists():
        try:
            with open(claude_desktop_config) as f:
                config = json.load(f)
                if "mcpServers" in config:
                    return Environment.CLAUDE_DESKTOP, {
                        "config_file": claude_desktop_config,
                        "config_dir": Path.home() / ".claude",
                    }
        except (json.JSONDecodeError, IOError):
            pass
    
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
    
    elif env == Environment.CLAUDE_CLI:
        info["setup_path"] = "~/.claude/claude.json"
        info["mcp_registration"] = "Register MCPs in ~/.claude/claude.json"
        info["notes"] = [
            "MCPs are global to your Claude CLI installation",
            "All projects share the same MCPs",
        ]
    
    elif env == Environment.CLAUDE_DESKTOP:
        info["setup_path"] = "~/.claude/claude_desktop_config.json"
        info["mcp_registration"] = "Add to 'mcpServers' object in config"
        info["notes"] = [
            "Claude Desktop uses the same config as Claude CLI",
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
        info["setup_path"] = "VS Code settings + ~/.claude/claude.json"
        info["mcp_registration"] = "Register in ~/.claude/claude.json"
        info["notes"] = [
            "Claude extension in VS Code uses ~/.claude config",
            "Configure MCPs the same way as Claude CLI",
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
            "Try running from Copilot CLI, Claude CLI, or Claude Desktop",
        ]
    
    return info
