#!/usr/bin/env bash
# SessionStart hook: keeps the pde-jsm MCP server's venv (and .env) in sync.
# Runs on every session start but only reinstalls when requirements.txt changed,
# so it's cheap on the common path.
#
# Uses CLAUDE_PLUGIN_ROOT (not CLAUDE_PLUGIN_DATA) so this works under both
# Claude Code and Copilot CLI: Copilot injects CLAUDE_PLUGIN_ROOT/PLUGIN_ROOT/
# COPILOT_PLUGIN_ROOT (all equal) for compatibility, but has no equivalent of
# Claude Code's separate, update-persistent CLAUDE_PLUGIN_DATA directory.
set -euo pipefail

PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-${PLUGIN_ROOT:-${COPILOT_PLUGIN_ROOT:-}}}"
if [ -z "$PLUGIN_ROOT" ]; then
  echo "bootstrap-venv.sh: no CLAUDE_PLUGIN_ROOT/PLUGIN_ROOT/COPILOT_PLUGIN_ROOT set, skipping" >&2
  exit 0
fi

MCP_SERVER_DIR="$PLUGIN_ROOT/mcp-servers/pde-jsm"
REQ_FILE="$MCP_SERVER_DIR/requirements.txt"
VENV_DIR="$PLUGIN_ROOT/.venv"
INSTALLED_MARKER="$PLUGIN_ROOT/.venv-requirements.installed"

if [ ! -d "$VENV_DIR" ]; then
  python3 -m venv "$VENV_DIR"
fi

if [ ! -f "$INSTALLED_MARKER" ] || ! diff -q "$REQ_FILE" "$INSTALLED_MARKER" >/dev/null 2>&1; then
  "$VENV_DIR/bin/pip" install --quiet --upgrade pip
  "$VENV_DIR/bin/pip" install --quiet -r "$REQ_FILE"
  cp "$REQ_FILE" "$INSTALLED_MARKER"
fi

# Materialize credentials into a .env next to app.py (its dotenv fallback path),
# but only if Claude Code's userConfig actually supplied any — otherwise leave
# whatever's already there alone (e.g. written by hand, or by the
# setup-provider-hub skill on Copilot CLI, which has no userConfig equivalent
# and would have its .env silently wiped every session without this guard).
ENV_FILE="$MCP_SERVER_DIR/.env"
if [ -n "${CLAUDE_PLUGIN_OPTION_ATLASSIAN_EMAIL:-}${CLAUDE_PLUGIN_OPTION_ATLASSIAN_API_TOKEN:-}${CLAUDE_PLUGIN_OPTION_EMAIL_USERNAME:-}${CLAUDE_PLUGIN_OPTION_EMAIL_PASSWORD:-}" ]; then
  {
    [ -n "${CLAUDE_PLUGIN_OPTION_ATLASSIAN_EMAIL:-}" ] && echo "ATLASSIAN_EMAIL=${CLAUDE_PLUGIN_OPTION_ATLASSIAN_EMAIL}"
    [ -n "${CLAUDE_PLUGIN_OPTION_ATLASSIAN_API_TOKEN:-}" ] && echo "ATLASSIAN_API_TOKEN=${CLAUDE_PLUGIN_OPTION_ATLASSIAN_API_TOKEN}"
    [ -n "${CLAUDE_PLUGIN_OPTION_EMAIL_USERNAME:-}" ] && echo "EMAIL_USERNAME=${CLAUDE_PLUGIN_OPTION_EMAIL_USERNAME}"
    [ -n "${CLAUDE_PLUGIN_OPTION_EMAIL_PASSWORD:-}" ] && echo "EMAIL_PASSWORD=${CLAUDE_PLUGIN_OPTION_EMAIL_PASSWORD}"
    true
  } > "$ENV_FILE"
  chmod 600 "$ENV_FILE"
fi
