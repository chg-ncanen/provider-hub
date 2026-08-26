#!/usr/bin/env bash
# SessionStart hook: mirrors this plugin's userConfig credentials into a
# .env file at the plugin root, for orchestrator.py/worker.py to read.
#
# Why this exists: Claude Code exports userConfig only as
# CLAUDE_PLUGIN_OPTION_<KEY> env vars to hook processes and MCP/LSP server
# subprocesses — never to a Bash tool call made during skill execution.
# ticket-orchestrator and ticket-worker are invoked via plain Bash
# (`python3 .../orchestrator.py`), not as a hook or an MCP server, so
# without this relay they have no way to see ATLASSIAN_EMAIL/
# ATLASSIAN_API_TOKEN/REPOS_DIR at all, no matter what's configured in this
# plugin's userConfig. Same pattern as pde-ops-tools/scripts/
# bootstrap-deps.sh's identical relay for resolve-duplicate-contact-alerts/
# run.py.
#
# Runs on every session start but only writes when userConfig actually
# supplied something, so it's a no-op (and doesn't clobber a hand-edited
# .env) on Copilot CLI, which has no userConfig / CLAUDE_PLUGIN_OPTION_*.
#
# Uses CLAUDE_PLUGIN_ROOT (not CLAUDE_PLUGIN_DATA) so this works under both
# Claude Code and Copilot CLI — see bootstrap-deps.sh's header comment in
# pde-ops-tools for why.
set -euo pipefail

PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-${PLUGIN_ROOT:-${COPILOT_PLUGIN_ROOT:-}}}"
if [ -z "$PLUGIN_ROOT" ]; then
  echo "bootstrap-env.sh: no CLAUDE_PLUGIN_ROOT/PLUGIN_ROOT/COPILOT_PLUGIN_ROOT set, skipping" >&2
  exit 0
fi

ENV_FILE="$PLUGIN_ROOT/.env"
if [ -n "${CLAUDE_PLUGIN_OPTION_ATLASSIAN_EMAIL:-}${CLAUDE_PLUGIN_OPTION_ATLASSIAN_API_TOKEN:-}${CLAUDE_PLUGIN_OPTION_REPOS_DIR:-}${CLAUDE_PLUGIN_OPTION_CONFLUENCE_SPACE_KEY:-}${CLAUDE_PLUGIN_OPTION_CONFLUENCE_PARENT_PAGE_ID:-}${CLAUDE_PLUGIN_OPTION_CONFLUENCE_SYNC_ENABLED:-}${CLAUDE_PLUGIN_OPTION_DRAFT_PR_ENABLED:-}${CLAUDE_PLUGIN_OPTION_COPILOT_REVIEW_ENABLED:-}" ]; then
  {
    # Preserve any line this hook doesn't manage, rather than truncating the
    # whole file down to just these keys. Written to a temp file first:
    # reading and truncating the same file in one redirect can read back an
    # already-empty file.
    if [ -f "$ENV_FILE" ]; then
      grep -vE '^(ATLASSIAN_EMAIL|ATLASSIAN_API_TOKEN|REPOS_DIR|CONFLUENCE_SPACE_KEY|CONFLUENCE_PARENT_PAGE_ID|CONFLUENCE_SYNC_ENABLED|DRAFT_PR_ENABLED|COPILOT_REVIEW_ENABLED)=' "$ENV_FILE" || true
    fi
    [ -n "${CLAUDE_PLUGIN_OPTION_ATLASSIAN_EMAIL:-}" ] && echo "ATLASSIAN_EMAIL=${CLAUDE_PLUGIN_OPTION_ATLASSIAN_EMAIL}"
    [ -n "${CLAUDE_PLUGIN_OPTION_ATLASSIAN_API_TOKEN:-}" ] && echo "ATLASSIAN_API_TOKEN=${CLAUDE_PLUGIN_OPTION_ATLASSIAN_API_TOKEN}"
    [ -n "${CLAUDE_PLUGIN_OPTION_REPOS_DIR:-}" ] && echo "REPOS_DIR=${CLAUDE_PLUGIN_OPTION_REPOS_DIR}"
    [ -n "${CLAUDE_PLUGIN_OPTION_CONFLUENCE_SPACE_KEY:-}" ] && echo "CONFLUENCE_SPACE_KEY=${CLAUDE_PLUGIN_OPTION_CONFLUENCE_SPACE_KEY}"
    [ -n "${CLAUDE_PLUGIN_OPTION_CONFLUENCE_PARENT_PAGE_ID:-}" ] && echo "CONFLUENCE_PARENT_PAGE_ID=${CLAUDE_PLUGIN_OPTION_CONFLUENCE_PARENT_PAGE_ID}"
    [ -n "${CLAUDE_PLUGIN_OPTION_CONFLUENCE_SYNC_ENABLED:-}" ] && echo "CONFLUENCE_SYNC_ENABLED=${CLAUDE_PLUGIN_OPTION_CONFLUENCE_SYNC_ENABLED}"
    [ -n "${CLAUDE_PLUGIN_OPTION_DRAFT_PR_ENABLED:-}" ] && echo "DRAFT_PR_ENABLED=${CLAUDE_PLUGIN_OPTION_DRAFT_PR_ENABLED}"
    [ -n "${CLAUDE_PLUGIN_OPTION_COPILOT_REVIEW_ENABLED:-}" ] && echo "COPILOT_REVIEW_ENABLED=${CLAUDE_PLUGIN_OPTION_COPILOT_REVIEW_ENABLED}"
    true
  } > "$ENV_FILE.tmp"
  mv "$ENV_FILE.tmp" "$ENV_FILE"
  chmod 600 "$ENV_FILE" 2>/dev/null || true
fi
