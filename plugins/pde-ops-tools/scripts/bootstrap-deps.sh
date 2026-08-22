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
    # Can't hand this off to manage_companions.py's dep-guidance for a decisive
    # OS-aware command the way sf/gcx get — that script is Python, so if
    # there's no python3/python on PATH at all, it can't run either. Give the
    # same kind of decisive, OS-aware guidance here in bash instead of a bare
    # "not found" with no next step.
    {
      echo "bootstrap-deps.sh: no python3/python found on PATH — pde-mcp can't start without it."
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
  # rest of venv creation "succeeds" (verified against a real Claude Code
  # SessionStart hook run — pip and its installed console scripts showed up
  # fine, but bin/python and bin/python3 were the only things missing).
  #
  # `|| true` is required, not cosmetic: on a system where ensurepip's wheel
  # data isn't installed (e.g. Debian/Ubuntu without python3.X-venv), this
  # command still creates the directory and the python binary but exits
  # non-zero — under `set -e` that would otherwise kill the script right
  # here, before it ever reaches the get-pip.py self-heal below, forcing a
  # second invocation to actually finish the job (verified: a real run
  # needed to be run twice to produce a working venv without this).
  "$system_python" -m venv --copies "$VENV_DIR" || true
fi

# On Python 3.14 + a UTF-8 filesystem, this venv's bin/ will also contain a
# 4th interpreter copy named 𝜋thon (mathematical italic pi, not "p") — a real
# CPython stdlib easter egg (venv/__init__.py's setup_python(), not anything
# this script adds), byte-identical to python/python3/python3.14. Expected
# and harmless; not a sign of tampering.

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
# `|| true` is required here, not cosmetic: this line sits in the if-BODY, not
# an if/while condition, so under `set -e` a nonzero exit (curl failing, no
# network reaching bootstrap.pypa.io, etc.) would otherwise kill the whole
# script right here — skipping the `rm -f` below *and* the failure-detection
# block that follows, permanently stranding a half-built venv (python present,
# pip absent) that the `[ ! -d "$VENV_DIR" ]` check above never retries
# (verified: this exact failure silently bricked a real venv for a full day).
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

if [ ! -f "$INSTALLED_MARKER" ] || [ "$(cat "$REQ_FILE")" != "$(cat "$INSTALLED_MARKER")" ]; then
  "$VENV_DIR/bin/pip" install --quiet --upgrade pip
  "$VENV_DIR/bin/pip" install --quiet -r "$REQ_FILE"
  cp "$REQ_FILE" "$INSTALLED_MARKER"
else
  # requirements.txt's *text* is unchanged, but a `pkg @ git+URL` line with no
  # pinned commit (tracking a branch, e.g. pde-ops-api tracking main) can still
  # have new commits upstream that this venv has never picked up — pip only
  # ever resolves a git ref at install time, so the marker-equality check above
  # can never detect that kind of drift on its own. (Verified the hard way:
  # two separate pde-ops-api fixes were committed and pushed, and neither ever
  # reached this venv, because this file's text never changed to trigger a
  # reinstall — they were silently running the commit that was HEAD the very
  # first time this venv was provisioned.) Compare each such dependency's
  # current remote commit against what's actually installed (from pip's own
  # direct_url.json record) and re-fetch just the ones that drifted. This is
  # one `git ls-remote` per git+ dependency (no full clone) — cheap enough to
  # run every session, and it's the only way to actually detect this.
  stale_specs="$("$VENV_DIR/bin/python" - "$REQ_FILE" "$VENV_DIR" <<'PYEOF'
import glob
import json
import os
import re
import subprocess
import sys

req_file, venv_dir = sys.argv[1], sys.argv[2]
site_packages = glob.glob(os.path.join(venv_dir, "lib", "python3.*", "site-packages")) + \
    glob.glob(os.path.join(venv_dir, "Lib", "site-packages"))

# name @ git+URL[@ref]  (an optional #subdirectory=... fragment is part of URL)
pattern = re.compile(r"^([A-Za-z0-9_.-]+)\s*@\s*git\+([^\s@#]+(?:#[^\s@]+)?)(?:@([^\s#]+))?\s*$")

for line in open(req_file):
    spec = line.strip()
    match = pattern.match(spec)
    if not match:
        continue
    name, url, ref = match.group(1), match.group(2), match.group(3)
    base_url = url.split("#", 1)[0]
    # A full 40-char commit hash is a real pin — nothing can drift under it.
    if ref and re.fullmatch(r"[0-9a-fA-F]{40}", ref):
        continue
    try:
        remote_out = subprocess.run(
            ["git", "ls-remote", base_url, ref or "HEAD"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout
        remote_commit = remote_out.split()[0] if remote_out.strip() else None
    except Exception:
        continue  # best-effort — a network hiccup here should never block the session
    if not remote_commit:
        continue

    installed_commit = None
    for site_dir in site_packages:
        for direct_url_path in glob.glob(
            os.path.join(site_dir, f"{name.replace('-', '_')}-*.dist-info", "direct_url.json")
        ):
            try:
                installed_commit = json.load(open(direct_url_path)).get("vcs_info", {}).get("commit_id")
            except Exception:
                pass
            break
        if installed_commit:
            break

    if installed_commit != remote_commit:
        print(spec)
PYEOF
  )"
  if [ -n "$stale_specs" ]; then
    while IFS= read -r spec; do
      [ -n "$spec" ] && "$VENV_DIR/bin/pip" install --quiet --force-reinstall --no-deps "$spec"
    done <<< "$stale_specs"
  fi
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
