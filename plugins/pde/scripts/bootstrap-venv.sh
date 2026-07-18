#!/usr/bin/env bash
# SessionStart hook: keeps the pde-jsm MCP server's venv (and .env) in sync.
# Runs on every session start but only reinstalls when requirements.txt changed,
# so it's cheap on the common path.
set -euo pipefail

PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:?CLAUDE_PLUGIN_ROOT not set}"
DATA_DIR="${CLAUDE_PLUGIN_DATA:?CLAUDE_PLUGIN_DATA not set}"
REQ_FILE="$PLUGIN_ROOT/mcp-servers/pde-jsm/requirements.txt"
VENV_DIR="$DATA_DIR/venv"
INSTALLED_MARKER="$DATA_DIR/.requirements.installed"

mkdir -p "$DATA_DIR"

if [ ! -d "$VENV_DIR" ]; then
  python3 -m venv "$VENV_DIR"
fi

if [ ! -f "$INSTALLED_MARKER" ] || ! diff -q "$REQ_FILE" "$INSTALLED_MARKER" >/dev/null 2>&1; then
  "$VENV_DIR/bin/pip" install --quiet --upgrade pip
  "$VENV_DIR/bin/pip" install --quiet -r "$REQ_FILE"
  cp "$REQ_FILE" "$INSTALLED_MARKER"
fi

# Materialize credentials collected via plugin.json's userConfig into a .env
# file app.py already knows how to load. CLAUDE_PLUGIN_OPTION_* vars are only
# set when the corresponding userConfig value has been configured.
ENV_FILE="$DATA_DIR/.env"
{
  [ -n "${CLAUDE_PLUGIN_OPTION_ATLASSIAN_EMAIL:-}" ] && echo "ATLASSIAN_EMAIL=${CLAUDE_PLUGIN_OPTION_ATLASSIAN_EMAIL}"
  [ -n "${CLAUDE_PLUGIN_OPTION_ATLASSIAN_API_TOKEN:-}" ] && echo "ATLASSIAN_API_TOKEN=${CLAUDE_PLUGIN_OPTION_ATLASSIAN_API_TOKEN}"
  [ -n "${CLAUDE_PLUGIN_OPTION_EMAIL_USERNAME:-}" ] && echo "EMAIL_USERNAME=${CLAUDE_PLUGIN_OPTION_EMAIL_USERNAME}"
  [ -n "${CLAUDE_PLUGIN_OPTION_EMAIL_PASSWORD:-}" ] && echo "EMAIL_PASSWORD=${CLAUDE_PLUGIN_OPTION_EMAIL_PASSWORD}"
  true
} > "$ENV_FILE"
chmod 600 "$ENV_FILE"
