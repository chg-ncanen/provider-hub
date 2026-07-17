# setup-provider-hub

**Zero-dependency setup tool for provider-hub.**

This tool automatically detects your environment (Copilot CLI, Claude CLI, Claude Desktop, VS Code, etc.) and configures all provider-hub dependencies. It backs up all configuration files before making changes, so you can easily revert if needed.

## Quick Start

```bash
# From repo root
python3 bootstrap.py --check          # See what's missing
python3 bootstrap.py                  # Interactive setup menu
python3 bootstrap.py --restore        # Revert to previous config

# Or directly
cd ai-skills/team/pde/setup-provider-hub
python3 setup.py --check              # Check status
python3 setup.py --backups            # List backups
python3 setup.py --restore            # Restore
```

## What It Does

### Detects Your Environment

- **Copilot CLI** — Configures `.copilot-config.json`
- **Claude CLI / Claude Desktop** — Configures `~/.claude/claude*.json`
- **VS Code with extensions** — Configures appropriate tool
- **Other clients** — Provides manual guidance

### Automatic Backups

Before modifying any configuration:
- Backs up existing files with timestamp
- Stores in `~/.provider-hub-backups/`
- Creates manifest for easy restore
- Never overwrite a file without backing it up first

### Status Checking

```bash
python3 setup.py --check
```

Reports on:
- Detected environment
- System tools (Python, npm, git)
- CLI tools (Salesforce CLI)
- Registered MCPs
- Installed Python packages
- Required environment variables

## Backups

List available backups:

```bash
python3 setup.py --backups
```

Restore from backup (interactive):

```bash
python3 setup.py --restore
```

Backups stored in: `~/.provider-hub-backups/`

## What Gets Configured

### MCPs
- **pde-jsm** — In-repo MCP for Jira/Slack
- **salesforce-prod** — Built-in Copilot CLI MCP for Salesforce

### CLI Tools
- **Salesforce CLI (sf)** — `npm install -g @salesforce/cli`

### Python Packages
- **requests** — HTTP library
- **python-dotenv** — .env file management
- **mcp** — MCP SDK

### Environment Variables
- `ATLASSIAN_EMAIL` — Required for Jira API
- `ATLASSIAN_API_TOKEN` — Required for Jira API
- `EMAIL_USERNAME` — Optional for email features
- `EMAIL_PASSWORD` — Optional for email features

## Files

- `setup.py` — Main setup script
- `environment.py` — Environment detection module
- `SKILL.md` — Full skill documentation
- `README.md` — This file

## Zero Dependencies

Uses Python standard library only. Works in any environment with Python 3.9+.

## Troubleshooting

### Can't detect environment?

Check what environment variables are set:

```bash
echo $COPILOT_CLI
echo $CLAUDE_CLI
echo $VSCODE_PID
```

### Config file not found?

The setup tool checks for:
- Copilot CLI: `~/.copilot-config.json`
- Claude CLI: `~/.claude/claude.json`
- Claude Desktop: `~/.claude/claude_desktop_config.json`

Create one if missing or run setup to initialize.

### Need to revert changes?

```bash
python3 setup.py --restore    # Choose from available backups
```

All backups kept in `~/.provider-hub-backups/` indefinitely.
