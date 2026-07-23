#!/usr/bin/env bash
# SessionStart hook: keeps the pde-mcp MCP server's venv in sync so its
# command (${CLAUDE_PLUGIN_ROOT}/.venv/bin/python in .mcp.json) actually
# exists and has its deps installed, and mirrors Claude Code's userConfig
# credentials into pde-mcp's own .env. Runs on every session start but only
# does real work when something's actually missing/changed, so it's cheap
# on the common path.
#
# The .env mirroring exists ONLY for resolve-duplicate-contact-alerts/run.py:
# unlike pde-mcp itself (which gets credentials straight from .mcp.json's
# ${user_config.*} substitution when Claude Code spawns it), run.py is
# invoked directly and never goes through that path, so without this it has
# no credential source at all on Claude Code. Companion tooling (the `sf`
# CLI, salesforce-prod, etc.) deliberately stays out of this hook — that's
# the setup-companion-tools skill's job, since none of it is required just
# to start pde-mcp.
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

MCP_SERVER_DIR="$PLUGIN_ROOT/mcp-servers/pde-mcp"
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
  # --copies (not the default symlinks): some hook sandboxes block symlink
  # creation, which otherwise silently leaves bin/python missing while the
  # rest of venv creation "succeeds" (verified against a real Claude Code
  # SessionStart hook run — pip and its installed console scripts showed up
  # fine, but bin/python and bin/python3 were the only things missing).
  "$(find_system_python)" -m venv --copies "$VENV_DIR"
fi

# Normalize to a `bin/` layout regardless of what the venv module produced.
if [ ! -x "$VENV_DIR/bin/python" ] && [ -x "$VENV_DIR/Scripts/python.exe" ]; then
  mkdir -p "$VENV_DIR/bin"
  cp "$VENV_DIR/Scripts/python.exe" "$VENV_DIR/bin/python"
  cp "$VENV_DIR/Scripts/pip.exe" "$VENV_DIR/bin/pip"
fi

# Debian/Ubuntu (including WSL Ubuntu) strips ensurepip's bundled pip/setuptools
# wheel data out of the base python3 package — only the version-specific
# python3.X-venv package includes it — so `python -m venv` above can create the
# directory and the python binary but silently leave bin/pip missing. Once that
# half-broken venv exists, the `[ ! -d "$VENV_DIR" ]` check never retries it, so
# every subsequent session would otherwise fail identically forever (verified
# against a real broken venv: python/python3 present, pip entirely absent).
# Self-heal via get-pip.py first — it downloads pip/setuptools straight from
# PyPI instead of needing the system's bundled wheel data, so it needs no root.
if [ ! -x "$VENV_DIR/bin/pip" ]; then
  curl -fsSL https://bootstrap.pypa.io/get-pip.py -o "$VENV_DIR/get-pip.py" 2>/dev/null \
    && "$VENV_DIR/bin/python" "$VENV_DIR/get-pip.py" --quiet 2>/dev/null
  rm -f "$VENV_DIR/get-pip.py"
fi

if [ ! -x "$VENV_DIR/bin/pip" ]; then
  PYVER="$("$VENV_DIR/bin/python" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")' 2>/dev/null || echo "3")"
  rm -rf "$VENV_DIR" "$INSTALLED_MARKER"
  {
    echo "bootstrap-deps.sh: couldn't provision pip into the venv — bin/pip is missing, and"
    echo "the get-pip.py fallback didn't work either (likely no network access to"
    echo "bootstrap.pypa.io). This usually means the python3-venv system package isn't"
    echo "installed. Run this yourself (needs root), then restart your session:"
    echo "  sudo apt install python${PYVER}-venv"
  } >&2
  exit 1
fi

if [ ! -f "$INSTALLED_MARKER" ] || [ "$(cat "$REQ_FILE")" != "$(cat "$INSTALLED_MARKER")" ]; then
  "$VENV_DIR/bin/pip" install --quiet --upgrade pip
  "$VENV_DIR/bin/pip" install --quiet -r "$REQ_FILE"
  cp "$REQ_FILE" "$INSTALLED_MARKER"
fi

# Mirror userConfig credentials into pde-mcp/.env for run.py's benefit (see
# header comment) — only if Claude Code actually supplied any, so this is a
# no-op on Copilot CLI (no userConfig, CLAUDE_PLUGIN_OPTION_* is never set
# there) and doesn't wipe a .env someone created by hand in that case.
ENV_FILE="$MCP_SERVER_DIR/.env"
if [ -n "${CLAUDE_PLUGIN_OPTION_ATLASSIAN_EMAIL:-}${CLAUDE_PLUGIN_OPTION_ATLASSIAN_API_TOKEN:-}${CLAUDE_PLUGIN_OPTION_EMAIL_USERNAME:-}${CLAUDE_PLUGIN_OPTION_EMAIL_PASSWORD:-}" ]; then
  {
    # Preserve any line this hook doesn't manage — e.g. .env.example's
    # EMAIL_IMAP_HOST/EMAIL_SMTP_HOST overrides for non-Gmail providers —
    # rather than truncating the whole file down to just these 4 keys.
    # Written to a temp file first: reading and truncating the same file in
    # one redirect can read back an already-empty file.
    if [ -f "$ENV_FILE" ]; then
      grep -vE '^(ATLASSIAN_EMAIL|ATLASSIAN_API_TOKEN|EMAIL_USERNAME|EMAIL_PASSWORD)=' "$ENV_FILE" || true
    fi
    [ -n "${CLAUDE_PLUGIN_OPTION_ATLASSIAN_EMAIL:-}" ] && echo "ATLASSIAN_EMAIL=${CLAUDE_PLUGIN_OPTION_ATLASSIAN_EMAIL}"
    [ -n "${CLAUDE_PLUGIN_OPTION_ATLASSIAN_API_TOKEN:-}" ] && echo "ATLASSIAN_API_TOKEN=${CLAUDE_PLUGIN_OPTION_ATLASSIAN_API_TOKEN}"
    [ -n "${CLAUDE_PLUGIN_OPTION_EMAIL_USERNAME:-}" ] && echo "EMAIL_USERNAME=${CLAUDE_PLUGIN_OPTION_EMAIL_USERNAME}"
    [ -n "${CLAUDE_PLUGIN_OPTION_EMAIL_PASSWORD:-}" ] && echo "EMAIL_PASSWORD=${CLAUDE_PLUGIN_OPTION_EMAIL_PASSWORD}"
    true
  } > "$ENV_FILE.tmp"
  mv "$ENV_FILE.tmp" "$ENV_FILE"
  chmod 600 "$ENV_FILE" 2>/dev/null || true
fi
