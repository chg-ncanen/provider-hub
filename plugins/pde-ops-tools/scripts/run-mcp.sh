#!/usr/bin/env bash
# .mcp.json launches this instead of .venv/bin/python directly, so pde-mcp
# self-heals its venv on every launch attempt, not just at SessionStart.
#
# Why this matters: SessionStart only fires for a brand-new session — it
# never re-runs for a session that's already been running when this plugin's
# cache directory gets resynced mid-session (e.g. any push to another plugin
# sharing this marketplace's repo bumps every plugin's commit-pinned cache
# path, this one included, even though its own files didn't change). The
# resynced cache directory then has no .venv at all until either a fresh
# session starts or something provisions it, so without this wrapper Claude
# Code fails to connect (ENOENT) until the user manually restarts.
#
# bootstrap-deps.sh is itself idempotent and cheap when nothing's missing
# (see its own header comment), so calling it unconditionally on every
# launch has no real cost on the common path — this generalizes SessionStart
# fixing pde-mcp only work into "everything that could actually run
# pde-mcp fixes it first."
set -euo pipefail

PLUGIN_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLAUDE_PLUGIN_ROOT="$PLUGIN_ROOT" "$PLUGIN_ROOT/scripts/bootstrap-deps.sh"
exec "$PLUGIN_ROOT/.venv/bin/python" "$PLUGIN_ROOT/mcp-servers/pde-mcp/app.py"
