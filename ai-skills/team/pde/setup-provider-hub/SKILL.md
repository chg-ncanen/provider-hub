# setup-provider-hub Skill

**Configure provider-hub dependencies and environment with zero dependencies.**

Automatically detects your client environment (Copilot CLI, Claude CLI, Claude Desktop, VS Code, etc.) and provides appropriate setup guidance. Backs up all configuration files before making changes so you can easily revert.

## When to Use This Skill

- Setting up provider-hub for the first time
- Installing or configuring MCPs, tools, and dependencies
- Verifying your environment is ready to use provider-hub skills
- Restoring configuration if something goes wrong

## Zero Dependencies

This skill uses Python standard library only—no pip installs required. It auto-detects your environment and provides context-aware instructions.

## Features

### Environment Detection

Automatically detects:
- **Copilot CLI** — Project-scoped MCP configuration
- **Claude CLI / Claude Desktop** — Global MCP configuration
- **VS Code with Copilot** — Uses Copilot CLI config
- **VS Code with Claude** — Uses Claude config
- **Copilot Desktop** — Limited MCP support
- **Unknown environment** — Falls back to manual guidance

### Automatic Backup

All configuration files are backed up before modification:
- Located in `~/.provider-hub-backups/`
- Timestamped backups with manifests
- Easy restore via `--restore` flag

### Status Checking

Get a complete report of what's installed and what's missing:

```bash
python setup.py --check
```

Shows:
- Your detected environment
- System prerequisites (Python, npm, git)
- CLI tools (Salesforce CLI)
- Registered MCPs
- Installed Python packages
- Environment variables

### Interactive Menu

Run without arguments for a guided setup:

```bash
python setup.py
```

Choose from:
1. Check status
2. List backups
3. Restore from backup
4. Install system prerequisites
5. Install Salesforce CLI
6. Register pde-jsm MCP
7. Register salesforce-prod MCP
8. Install Python packages
9. Set environment variables

## Usage

### In Copilot CLI

```bash
/skill setup-provider-hub
```

### Standalone

```bash
cd /path/to/provider-hub/ai-skills/team/pde/setup-provider-hub
python setup.py --check          # View current status
python setup.py --backups        # List available backups
python setup.py --restore        # Restore from backup
python setup.py                  # Interactive menu
```

## What Gets Backed Up

Before any modification, backups are created for:

- `~/.copilot-config.json` — Copilot CLI config
- `~/.claude/claude.json` — Claude CLI config
- `~/.claude/claude_desktop_config.json` — Claude Desktop config
- Environment variable files

Browse backups:

```bash
ls -la ~/.provider-hub-backups/
```

## Supported Installations

### MCPs
- **pde-jsm** — PDE Jira/Slack MCP (in repo)
- **salesforce-prod** — Salesforce CLI integration (built-in to Copilot CLI)

### CLI Tools
- **Salesforce CLI (sf)** — `npm install -g @salesforce/cli`

### Python Packages
- **requests** — HTTP library
- **python-dotenv** — Environment variable management
- **mcp** — MCP SDK

### Environment Variables
- `ATLASSIAN_EMAIL` — Jira API email
- `ATLASSIAN_API_TOKEN` — Jira API token
- `EMAIL_USERNAME` — For pde-ops-api email features (optional)
- `EMAIL_PASSWORD` — For pde-ops-api email features (optional)

## Troubleshooting

### "Environment unknown"

The tool couldn't detect your client. Manually check:

```bash
# Copilot CLI
echo $COPILOT_CLI

# Claude CLI
echo $CLAUDE_CLI

# Claude Desktop
ls ~/.claude/claude_desktop_config.json

# VS Code
echo $VSCODE_PID
```

### "MCP registration failed"

Check that the config file exists and is writable:

```bash
# Copilot CLI
cat ~/.copilot-config.json

# Claude CLI
cat ~/.claude/claude.json

# Claude Desktop
cat ~/.claude/claude_desktop_config.json
```

### Need to Revert Changes?

List and restore from backup:

```bash
python setup.py --backups     # See what's available
python setup.py --restore     # Choose what to restore
```

Backups are kept in `~/.provider-hub-backups/` indefinitely.

## Next Steps

After setup completes:

1. **Verify status** — `python setup.py --check`
2. **Test MCPs** — Use `/mcp list` in Copilot CLI or check Claude settings
3. **Try a skill** — Run `resolve-duplicate-contact-alerts` or another pde skill
4. **Check environment** — Make sure Jira API token and Salesforce org are accessible

## Questions or Issues?

- Check the `--check` status output
- Review setup scripts in this directory
- Check `environment.py` for environment detection logic
- Restore from backup if needed: `python setup.py --restore`
