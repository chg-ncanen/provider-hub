#!/usr/bin/env bash
# SessionStart hook: keeps the pde-jsm MCP server's venv (and .env) in sync,
# and best-effort installs the `sf` CLI the resolve-duplicate-contact-alerts
# skill needs. Runs on every session start but only does real work when
# something's actually missing/changed, so it's cheap on the common path.
#
# Uses CLAUDE_PLUGIN_ROOT (not CLAUDE_PLUGIN_DATA) so this works under both
# Claude Code and Copilot CLI: Copilot injects CLAUDE_PLUGIN_ROOT/PLUGIN_ROOT/
# COPILOT_PLUGIN_ROOT (all equal) for compatibility, but has no equivalent of
# Claude Code's separate, update-persistent CLAUDE_PLUGIN_DATA directory.
#
# Runs under Git Bash on Windows (the default hook shell there, and already
# required for Claude Desktop's Code tab). `python -m venv` still produces a
# Windows-layout venv (.venv\Scripts\python.exe) regardless of which shell
# invoked it, so after creating the venv we mirror python/pip into a `bin/`
# subdir when needed — keeping `.venv/bin/python` (referenced by .mcp.json)
# valid on every OS without giving .mcp.json itself any OS-specific branches.
set -euo pipefail

PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-${PLUGIN_ROOT:-${COPILOT_PLUGIN_ROOT:-}}}"
if [ -z "$PLUGIN_ROOT" ]; then
  echo "bootstrap-deps.sh: no CLAUDE_PLUGIN_ROOT/PLUGIN_ROOT/COPILOT_PLUGIN_ROOT set, skipping" >&2
  exit 0
fi

MCP_SERVER_DIR="$PLUGIN_ROOT/mcp-servers/pde-jsm"
REQ_FILE="$MCP_SERVER_DIR/requirements.txt"
VENV_DIR="$PLUGIN_ROOT/.venv"
INSTALLED_MARKER="$PLUGIN_ROOT/.venv-requirements.installed"

find_system_python() {
  if command -v python3 >/dev/null 2>&1; then
    echo "python3"
  elif command -v python >/dev/null 2>&1; then
    echo "python"
  else
    echo "bootstrap-deps.sh: no python3/python found on PATH" >&2
    exit 1
  fi
}

if [ ! -d "$VENV_DIR" ]; then
  "$(find_system_python)" -m venv "$VENV_DIR"
fi

# Normalize to a `bin/` layout regardless of what the venv module produced.
if [ ! -x "$VENV_DIR/bin/python" ] && [ -x "$VENV_DIR/Scripts/python.exe" ]; then
  mkdir -p "$VENV_DIR/bin"
  cp "$VENV_DIR/Scripts/python.exe" "$VENV_DIR/bin/python"
  cp "$VENV_DIR/Scripts/pip.exe" "$VENV_DIR/bin/pip"
fi

if [ ! -f "$INSTALLED_MARKER" ] || [ "$(cat "$REQ_FILE")" != "$(cat "$INSTALLED_MARKER")" ]; then
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
  chmod 600 "$ENV_FILE" 2>/dev/null || true
fi

# --- Salesforce CLI, needed by the resolve-duplicate-contact-alerts skill ---
# Best-effort and non-fatal: this whole block must never abort the script,
# since the pde-jsm MCP server itself doesn't depend on `sf` at all — only
# that one skill does, when a user actually invokes it.
{
  if ! command -v sf >/dev/null 2>&1; then
    if command -v npm >/dev/null 2>&1; then
      echo "bootstrap-deps.sh: installing Salesforce CLI (npm install -g @salesforce/cli)..." >&2
      npm install -g @salesforce/cli >/dev/null 2>&1 \
        && echo "bootstrap-deps.sh: sf CLI installed." >&2 \
        || echo "bootstrap-deps.sh: sf CLI install failed — install manually with 'npm install -g @salesforce/cli' if you need resolve-duplicate-contact-alerts." >&2
    else
      echo "bootstrap-deps.sh: npm not found, can't auto-install sf CLI — install Node.js/npm, then 'npm install -g @salesforce/cli', if you need resolve-duplicate-contact-alerts." >&2
    fi
  fi

  # Installing the binary is automatable; authenticating it is not (it's an
  # interactive browser OAuth flow) — just tell the user if 'prod' isn't set up.
  if command -v sf >/dev/null 2>&1; then
    if ! sf alias list --json 2>/dev/null | grep -q '"alias": *"prod"'; then
      echo "bootstrap-deps.sh: sf CLI has no 'prod' org alias — run 'sf org login web --alias prod' if you need resolve-duplicate-contact-alerts." >&2
    fi
  fi
} || true
