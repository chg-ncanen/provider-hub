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

# Guide the developer if credentials still aren't there — non-blocking, just
# a heads-up (Claude Code: nothing prompted userConfig yet, or the user
# skipped it; Copilot CLI: no prompt exists at all, so this is the only
# signal they'll get). pde-jsm itself still starts fine either way; only
# tool calls that need these will fail, later, when actually invoked.
_env_content=""
[ -f "$ENV_FILE" ] && _env_content="$(cat "$ENV_FILE" 2>/dev/null)"
if [[ "$_env_content" != *"ATLASSIAN_EMAIL="* ]] || [[ "$_env_content" != *"ATLASSIAN_API_TOKEN="* ]]; then
  echo "bootstrap-deps.sh: pde-jsm has no ATLASSIAN_EMAIL/ATLASSIAN_API_TOKEN configured yet — alert tools will fail until you do. Claude Code: run '/plugin configure pde@provider-hub'. Copilot CLI (no config prompt exists): copy mcp-servers/pde-jsm/.env.example to mcp-servers/pde-jsm/.env and fill it in." >&2
fi

# --- Salesforce CLI, needed by the resolve-duplicate-contact-alerts skill ---
# Best-effort and non-fatal: this whole block must never abort the script,
# since the pde-jsm MCP server itself doesn't depend on `sf` at all — only
# that one skill does, when a user actually invokes it.
{
  if ! command -v sf >/dev/null 2>&1; then
    if command -v npm >/dev/null 2>&1; then
      echo "bootstrap-deps.sh: installing Salesforce CLI (npm install -g @salesforce/cli)..." >&2
      # A hook has no TTY, so it can never prompt for a password — that's a
      # hard limit, not something to work around. Two safe (non-hanging)
      # attempts, then defer to a human if neither works:
      #  1. Plain install — works if the global npm prefix is user-writable.
      #  2. `sudo -n` (non-interactive) — succeeds silently if the user
      #     already has a cached sudo session or an admin pre-configured
      #     passwordless sudo for this exact command; fails instantly
      #     (never prompts) otherwise, verified directly against this
      #     machine's sudo.
      if npm install -g @salesforce/cli >/dev/null 2>&1; then
        echo "bootstrap-deps.sh: sf CLI installed." >&2
      elif command -v sudo >/dev/null 2>&1 && sudo -n npm install -g @salesforce/cli >/dev/null 2>&1; then
        echo "bootstrap-deps.sh: sf CLI installed (via pre-authorized sudo)." >&2
      else
        echo "bootstrap-deps.sh: sf CLI install failed (needs root and no cached/passwordless sudo is available — a hook can't prompt for a password). If you need resolve-duplicate-contact-alerts: ask whoever manages this machine to install it (or set up passwordless sudo for 'npm install -g @salesforce/cli'), or avoid needing root yourself with a user-owned npm prefix: 'npm config set prefix ~/.npm-global && export PATH=\$HOME/.npm-global/bin:\$PATH' (add that export to your shell profile), then 'npm install -g @salesforce/cli'." >&2
      fi
    else
      echo "bootstrap-deps.sh: npm not found, can't auto-install sf CLI — install Node.js/npm, then 'npm install -g @salesforce/cli', if you need resolve-duplicate-contact-alerts." >&2
    fi
  fi

  # Installing the binary is automatable; authenticating it is not (it's an
  # interactive browser OAuth flow) — just tell the user if 'prod' isn't set up.
  # Uses python3 (not grep) to check the alias list — a personal PATH override
  # broke a plain `diff` call earlier for the exact same reason: never assume
  # a generic CLI tool behaves the way you expect on someone else's machine.
  if command -v sf >/dev/null 2>&1; then
    if ! sf alias list --json 2>/dev/null | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)
sys.exit(0 if any(a.get('alias') == 'prod' for a in d.get('result', [])) else 1)
" 2>/dev/null; then
      echo "bootstrap-deps.sh: sf CLI has no 'prod' org alias — run 'sf org login web --alias prod' if you need resolve-duplicate-contact-alerts." >&2
    fi
  fi
} || true
