#!/usr/bin/env bash
# SessionStart hook: keeps this plugin's own venv in sync so worker.py/
# orchestrator.py/confluence_sync.py (invoked as
# "${CLAUDE_PLUGIN_ROOT}/.venv/bin/python" in ticket-worker/SKILL.md and
# ticket-orchestrator/SKILL.md) actually exist and have their deps
# installed. Runs on every session start but only does real work when
# something's actually missing/changed, so it's cheap on the common path.
#
# Same pattern as pde-ops-tools/scripts/bootstrap-deps.sh's identical venv
# bootstrap for pde-mcp — copied and adapted, not shared: each plugin is
# scoped to its own $CLAUDE_PLUGIN_ROOT and bundles its own copy of this
# script, so having one of these two plugins installed without the other
# is always safe. This copy drops two things the original needs that don't
# apply here: the git+URL drift-detection block (this plugin's
# requirements.txt only ever lists plain PyPI packages, never a
# `pkg @ git+URL` dependency), and the userConfig-to-.env credential
# relay (this plugin already has that as a separate concern in
# bootstrap-env.sh, since it's needed by scripts invoked directly via Bash
# rather than an MCP server started through .mcp.json's own
# ${user_config.*} substitution).
#
# Uses CLAUDE_PLUGIN_ROOT (not CLAUDE_PLUGIN_DATA) so this works under both
# Claude Code and Copilot CLI — see bootstrap-env.sh's header comment for
# why.
#
# Runs under Git Bash on Windows (the default hook shell there). `python -m
# venv` still produces a Windows-layout venv (.venv\Scripts\python.exe)
# regardless of which shell invoked it, so after creating the venv we
# mirror python/pip into a `bin/` subdir when needed — keeping
# `.venv/bin/python` (referenced by both SKILL.md files) valid on every OS.
set -euo pipefail

PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-${PLUGIN_ROOT:-${COPILOT_PLUGIN_ROOT:-}}}"
if [ -z "$PLUGIN_ROOT" ]; then
  echo "bootstrap-deps.sh: no CLAUDE_PLUGIN_ROOT/PLUGIN_ROOT/COPILOT_PLUGIN_ROOT set, skipping" >&2
  exit 0
fi

REQ_FILE="$PLUGIN_ROOT/requirements.txt"
VENV_DIR="$PLUGIN_ROOT/.venv"
INSTALLED_MARKER="$PLUGIN_ROOT/.venv-requirements.installed"

find_system_python() {
  if command -v python3 >/dev/null 2>&1; then
    echo "python3"
  elif command -v python >/dev/null 2>&1; then
    echo "python"
  else
    {
      echo "bootstrap-deps.sh: no python3/python found on PATH — this plugin can't run without it."
      case "$(uname -s 2>/dev/null || echo unknown)" in
        Linux)
          if [ -f /etc/os-release ] && grep -qiE 'debian|ubuntu' /etc/os-release; then
            echo "Install it yourself (needs root): sudo apt install python3"
          else
            echo "Install Python 3 via your distro's package manager (e.g. sudo dnf install"
            echo "python3, sudo pacman -S python), then restart your session."
          fi
          ;;
        Darwin)
          echo "Install it yourself: brew install python3 (no root needed on Homebrew-managed"
          echo "installs), then restart your session."
          ;;
        MINGW*|MSYS*|CYGWIN*)
          echo "Install it yourself: download the installer from https://python.org (check"
          echo "'Add python.exe to PATH' during install) or run: winget install Python.Python.3.12"
          echo "Then restart your session."
          ;;
        *)
          echo "Install Python 3 from https://python.org or your OS's package manager, then"
          echo "restart your session."
          ;;
      esac
    } >&2
    exit 1
  fi
}

if [ ! -d "$VENV_DIR" ]; then
  # Capture into a variable rather than using the substitution directly as a
  # command name: `exit 1` inside find_system_python only exits the command
  # substitution's own subshell, not this script, so calling
  # "$(find_system_python)" directly as a command would silently continue
  # past a missing python3/python with an empty command instead of actually
  # stopping. `|| exit 1` here does propagate the subshell's exit status.
  system_python="$(find_system_python)" || exit 1

  # --copies (not the default symlinks): some hook sandboxes block symlink
  # creation, which otherwise silently leaves bin/python missing while the
  # rest of venv creation "succeeds".
  #
  # `|| true` is required, not cosmetic: on a system where ensurepip's wheel
  # data isn't installed (e.g. Debian/Ubuntu without python3.X-venv), this
  # command still creates the directory and the python binary but exits
  # non-zero — under `set -e` that would otherwise kill the script right
  # here, before it ever reaches the get-pip.py self-heal below.
  "$system_python" -m venv --copies "$VENV_DIR" || true

  # On Python 3.14 + a UTF-8 filesystem, venv's own setup_python() (Lib/
  # venv/__init__.py, verified directly against CPython's source) also
  # creates a 4th interpreter copy named 𝜋thon (mathematical italic pi, not
  # "p") — byte-identical to python/python3/python3.14, an unexplained
  # CPython stdlib easter egg, not anything this script adds. Nothing
  # anywhere ever invokes the interpreter by that literal name — only
  # bin/python is referenced (see both SKILL.md files) — so instead of
  # leaving it for a future reader (human or agent) to independently
  # rediscover and re-verify this exact fact every time, remove it right
  # after creation. `|| true`: harmless if venv creation above didn't
  # actually get this far (e.g. the ensurepip-wheel-missing case below).
  rm -f "$VENV_DIR/bin/𝜋thon" || true
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
# every subsequent session would otherwise fail identically forever.
# Self-heal via get-pip.py first — it downloads pip/setuptools straight from
# PyPI instead of needing the system's bundled wheel data, so it needs no root.
# `|| true` is required here, not cosmetic: this line sits in the if-BODY, not
# an if/while condition, so under `set -e` a nonzero exit (curl failing, no
# network reaching bootstrap.pypa.io, etc.) would otherwise kill the whole
# script right here — skipping the `rm -f` below *and* the failure-detection
# block that follows, permanently stranding a half-built venv (python present,
# pip absent) that the `[ ! -d "$VENV_DIR" ]` check above never retries.
if [ ! -x "$VENV_DIR/bin/pip" ]; then
  curl -fsSL https://bootstrap.pypa.io/get-pip.py -o "$VENV_DIR/get-pip.py" 2>/dev/null \
    && "$VENV_DIR/bin/python" "$VENV_DIR/get-pip.py" --quiet 2>/dev/null \
    || true
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

# Re-install whenever requirements.txt's content differs from what's
# already installed — not just when the venv is missing. Without this, a
# future plugin upgrade that bumps a dependency version (or adds a new one)
# would silently keep running whatever was installed the very first time
# this venv was created, forever, since the venv directory already exists
# and the check above would never fire again.
if [ ! -f "$INSTALLED_MARKER" ] || [ "$(cat "$REQ_FILE")" != "$(cat "$INSTALLED_MARKER")" ]; then
  "$VENV_DIR/bin/pip" install --quiet --upgrade pip
  "$VENV_DIR/bin/pip" install --quiet -r "$REQ_FILE"
  cp "$REQ_FILE" "$INSTALLED_MARKER"
fi
