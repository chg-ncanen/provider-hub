#!/usr/bin/env python3
"""
Check status of, and install, one at a time, the optional companion
MCPs/plugins commonly used alongside PDE tooling — none of which are
bundled in the `pde` plugin itself (grafana/gcx, logrocket, atlassian,
salesforce-prod, salesforce-uat, launch-darkly). Driven by the
setup-companion-tools skill; never runs on its own.

Usage:
    python3 manage_companions.py status --cli claude|copilot
    python3 manage_companions.py install <service> --cli claude|copilot
    python3 manage_companions.py dep-guidance <dependency>
    python3 manage_companions.py dep-install <dependency>
"""
import argparse
import json
import os
import platform
import re
import shutil
import subprocess


def run(cmd, timeout=30):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except Exception as e:
        return 1, "", str(e)


def find_plugin_root(plugin_name, cli):
    """Locate an installed plugin's root directory without relying on
    CLAUDE_PLUGIN_ROOT/PLUGIN_ROOT/COPILOT_PLUGIN_ROOT — those are only set
    when Claude Code/Copilot spawn something *for* that plugin (a hook, its
    MCP server); a plain shell invocation of this script (which is how it's
    actually run) never has them. Mirrors how claude_plugin_installed/
    copilot_plugin_installed already detect installation, but also captures
    the install path those helpers only use internally.

    cwd-sensitive for project-scoped plugins: `claude plugin list --json`
    reports `enabled: false` and omits `mcpServers` for a project-scoped
    entry whenever the invoking process's cwd doesn't match the `projectPath`
    it was installed for (confirmed in a sandboxed `--scope project` install
    — the entry itself always appears, only `enabled`/`mcpServers` change).
    This function inherits whatever cwd this script was launched from, so the
    caller must not `cd` away from the user's real project directory first —
    see setup-companion-tools/SKILL.md's note on this."""
    if cli == "claude":
        rc, out, _ = run(["claude", "plugin", "list", "--json"])
        if rc != 0:
            return None
        try:
            data = json.loads(out)
        except Exception:
            return None
        for p in data:
            if p.get("id", "").split("@")[0] == plugin_name and p.get("enabled", True):
                return p.get("installPath")
        return None
    home = os.environ.get("COPILOT_HOME", os.path.expanduser("~/.copilot"))
    base = os.path.join(home, "installed-plugins")
    if not os.path.isdir(base):
        return None
    for marketplace in os.listdir(base):
        candidate = os.path.join(base, marketplace, plugin_name)
        if os.path.isdir(candidate):
            return candidate
    return None


def pde_mcp_status(cli):
    """Read-only health check for pde-mcp itself — not a companion service (it's
    bundled with the PDE Ops Tools plugin, set up by the SessionStart hook, not
    this script), but worth surfacing here since every companion tool sits
    alongside it. Mirrors the exact manual diagnosis steps used to track down a
    real broken-venv bug: locate the plugin root, check the venv's python
    exists, then actually try importing pde-mcp's dependencies rather than
    inferring from a marker file."""
    plugin_root = find_plugin_root("pde", cli)
    if not plugin_root:
        return {
            "ready": None,
            "detail": "Not installed — the PDE Ops Tools plugin (`pde`) wasn't found",
        }

    venv_python = os.path.join(plugin_root, ".venv", "bin", "python")
    if not os.path.exists(venv_python):
        return {
            "ready": False,
            "detail": "venv not found yet — restart your session so the SessionStart hook can build it",
        }

    mcp_dir = os.path.join(plugin_root, "mcp-servers", "pde-mcp")
    rc, out, err = run(
        [
            venv_python,
            "-c",
            f"import sys; sys.path.insert(0, {mcp_dir!r}); "
            "import mcp, api.jsm.client, api.mail.email_tool",
        ],
        timeout=15,
    )
    if rc == 0:
        return {"ready": True, "detail": "Bundled with the PDE Ops Tools plugin — venv and dependencies OK"}
    reason = (err or out).strip().splitlines()[-1] if (err or out).strip() else "unknown import error"
    return {
        "ready": False,
        "detail": f"dependencies broken — {reason} (restart your session to let the hook reinstall)",
    }


# Floor for the unified CLI (the merged sf/sfdx architecture, published as
# `@salesforce/cli` v2+) — @salesforce/mcp's `--orgs`/`--toolsets` flags need
# this, and don't exist on the pre-unification `sf` v1 wrapper or the legacy
# standalone `sfdx-cli` package. Seen in the wild: machines with an old `sf`
# already on PATH from months/years back, which look "installed" by a plain
# rc==0 check but don't actually work with this MCP server. Raise this if a
# stricter minimum for @salesforce/mcp itself ever gets published.
SF_MIN_VERSION = (2, 0, 0)

# npm's own documented fix for a root-owned default global prefix — verified working end-to-end
# (installed a real, current `sf` with zero sudo on a machine whose default prefix was root-owned).
NPM_USER_PREFIX = "~/.npm-global"
_SF_VERSION_RE = re.compile(r"@salesforce/cli/(\d+)\.(\d+)\.(\d+)")


def sf_version_info():
    """None if `sf` isn't runnable at all. Otherwise a dict with the raw
    `--version` text and, when it matches the expected `@salesforce/cli/x.y.z`
    format, a (major, minor, patch) tuple. An unparseable version string
    (`parsed: None`) means something unexpected answers to `sf` — treated as
    'too old' by sf_meets_min_version() below, not as unknown, since that's
    exactly the class of oddball/legacy install this check exists to catch."""
    rc, out, err = run(["sf", "--version"])
    if rc != 0:
        return None
    text = (out or err).strip()
    m = _SF_VERSION_RE.search(text)
    return {"raw": text, "parsed": tuple(int(x) for x in m.groups()) if m else None}


def sf_installed():
    return sf_version_info() is not None


def sf_meets_min_version():
    """True/False once `sf` is confirmed installed; None if it isn't installed
    at all (callers should check sf_installed() separately rather than
    inferring 'not installed' from a False here)."""
    info = sf_version_info()
    if info is None:
        return None
    return info["parsed"] is not None and info["parsed"] >= SF_MIN_VERSION


def sf_installed_via_brew():
    """Whether the currently-active `sf` came from the Homebrew cask, so an
    upgrade offer can safely use `brew upgrade` instead of colliding with a
    binary Homebrew doesn't actually own."""
    if not shutil.which("brew"):
        return False
    rc, _, _ = run(["brew", "list", "--cask", "salesforce-cli"])
    return rc == 0


def _sf_known_orgs():
    rc, out, _ = run(["sf", "org", "list", "--json"])
    if rc != 0:
        return []
    try:
        d = json.loads(out)
        return [o for group in d.get("result", {}).values() if isinstance(group, list) for o in group]
    except Exception:
        return []


def sf_connected_aliases():
    """Aliases whose org is actually live-connected right now — `sf alias
    list` (used previously) only proves an alias mapping was created at some
    point, not that the session behind it is still valid: a revoked or
    expired refresh token leaves the alias in place but the org unusable,
    the same class of false-positive `gcx_configured()` had before it was
    fixed to stop trusting an always-0 exit code. `sf org list --json`'s
    per-org `connectedStatus` (checked across every org group it returns:
    sandboxes, nonScratchOrgs, devHubs, scratchOrgs, other) is the live
    signal instead."""
    return {o.get("alias") for o in _sf_known_orgs() if o.get("connectedStatus") == "Connected"}


def sf_known_aliases():
    """Aliases sf knows about at all, regardless of whether their session is
    currently valid — i.e. whether `sf org login web --alias <alias>` has
    ever been run for it, as distinct from sf_connected_aliases()'s live
    connectedStatus check. An alias can be known (mapped, shows up here) but
    not connected (revoked/expired refresh token) — that gap is exactly what
    the Configured vs Connected columns in the status table distinguish."""
    return {o.get("alias") for o in _sf_known_orgs() if o.get("alias")}


# Org login endpoints — the one real difference between prod and uat, everything else about the
# sf CLI (install/upgrade/version-floor handling, MCP registration, lazy-auth) is identical and
# shared via the `alias`-parameterized functions below. Sandboxes are never reachable via the
# generic login.salesforce.com regardless of org config, so uat always needs an explicit
# --instance-url; prod gets one too here since this org's confirmed My Domain
# (chg.my.salesforce.com — also referenced in resolve-duplicate-contact-alerts for building prod
# record links) doesn't accept login via the generic login page either. Update only here if either
# ever changes — every other place that needs the login command calls sf_login_command(alias)
# rather than re-typing a URL.
SF_LOGIN_INSTANCE_URLS = {
    "prod": "https://chg.my.salesforce.com",
    "uat": "https://chg--uat.sandbox.my.salesforce.com",
}


def sf_login_command(alias):
    url = SF_LOGIN_INSTANCE_URLS.get(alias)
    command = f"sf org login web --alias {alias}"
    return f"{command} --instance-url {url}" if url else command


def _sf_service_hints(alias):
    """Shared ready_hint/post_install text for a Salesforce alias's SERVICES entry — the two
    aliases only ever differ in the login command sf_login_command() produces."""
    login_command = sf_login_command(alias)
    ready_hint = (
        f"Registered, but the sf CLI isn't logged into '{alias}' yet — run `{login_command}` "
        "to connect it."
    )
    post_install = (
        "Registers regardless of `sf` login state (its tools just error if called before "
        f"you're logged in) — run `{login_command}` if you haven't already."
    )
    return ready_hint, post_install


def sf_dependency_status(alias):
    """Status of the `sf` CLI dependency, scoped to one org alias (prod/uat) —
    each Salesforce service needs its own alias logged in, even though the
    CLI binary itself is shared.

    `blocking` when the CLI isn't installed at all, or is installed but below
    SF_MIN_VERSION — there'd be no way to ever log in usefully in either case.
    Not logged into `alias` yet is non-blocking: verified directly that
    `npx -y @salesforce/mcp --orgs <alias>` starts up and registers all its
    tools cleanly even against a nonexistent/never-authenticated alias (no
    crash, no broken half-registered state) — the actual auth failure only
    surfaces if a tool like `run_soql_query` is called before logging in, the
    same lazy-auth pattern as atlassian/logrocket/launch-darkly, not the
    "broken install" this used to assume."""
    if not sf_installed():
        return {
            "name": "sf CLI",
            "installed": False,
            "configured": False,
            "ready": False,
            "detail": "sf CLI not found on PATH",
            "blocking": True,
        }
    if not sf_meets_min_version():
        info = sf_version_info()
        current = info["raw"] if info["parsed"] is None else "v" + ".".join(map(str, info["parsed"]))
        min_str = "v" + ".".join(map(str, SF_MIN_VERSION))
        return {
            "name": "sf CLI",
            "installed": True,
            "configured": False,
            "ready": False,
            "detail": (
                f"sf CLI installed but too old ({current}, need {min_str}+) — @salesforce/mcp "
                "needs the unified CLI; run `dep-install sf` or see `dep-guidance sf` for the "
                "upgrade command"
            ),
            "blocking": True,
        }
    configured = alias in sf_known_aliases()
    ready = configured and alias in sf_connected_aliases()
    return {
        "name": "sf CLI",
        "installed": True,
        "configured": configured,
        "ready": ready,
        "detail": (
            f"sf CLI installed, logged into '{alias}'"
            if ready
            else f"sf CLI installed but not logged into '{alias}'"
            if not configured
            else f"sf CLI configured for '{alias}' but the session isn't live — run "
            f"`{sf_login_command(alias)}` again to reconnect it"
        ),
        "blocking": False,
    }


# Floor for the gcx CLI's own bundled Claude Code plugin. gcx has no vendor-declared minimum CLI
# version for the plugin — the CLI and plugin ship from the same repo at matching version tags
# (e.g. both currently 1.0.0), so this is pinned to the actual installed/confirmed-working version
# rather than an inferred one. Bump this whenever you deliberately move to a newer gcx release.
GCX_MIN_VERSION = (1, 0, 0)
_GCX_VERSION_RE = re.compile(r"gcx version (\d+)\.(\d+)\.(\d+)")


def gcx_version_info():
    """None if `gcx` isn't runnable at all. Otherwise a dict with the raw
    `--version` text and, when it matches the expected `gcx version x.y.z`
    format, a (major, minor, patch) tuple."""
    rc, out, err = run(["gcx", "--version"])
    if rc != 0:
        return None
    text = (out or err).strip()
    m = _GCX_VERSION_RE.search(text)
    return {"raw": text, "parsed": tuple(int(x) for x in m.groups()) if m else None}


def gcx_installed():
    return gcx_version_info() is not None


def gcx_meets_min_version():
    """True/False once `gcx` is confirmed installed; None if it isn't installed at all."""
    info = gcx_version_info()
    if info is None:
        return None
    return info["parsed"] is not None and info["parsed"] >= GCX_MIN_VERSION


def gcx_context_configured():
    """Whether gcx's *current* context is actually pointed at a target
    (has a `server` set) — distinct from gcx_configured()'s live connectivity
    check below, so a context that exists but whose login session died reads
    as 'configured but not connected' rather than 'not configured at all'.
    `gcx config list-contexts --output json` lists every context (e.g. a
    leftover empty `default` alongside a real one) with `current: true` on
    whichever one is active; a context with no `server` key at all has never
    been pointed anywhere (`gcx login` was never run for it)."""
    rc, out, _ = run(["gcx", "config", "list-contexts", "--output", "json"])
    if rc != 0:
        return False
    try:
        data = json.loads(out)
    except Exception:
        return False
    for ctx in data.get("contexts", []):
        if ctx.get("current"):
            return bool(ctx.get("server"))
    return False


def gcx_configured():
    # `gcx config check`'s exit code carries no signal either way — verified
    # both that it returns rc==0 against an empty/invalid config dir with no
    # context at all, AND rc==1 here with a perfectly fine *current* context
    # just because an unrelated, unused context (e.g. a leftover empty
    # `default`) happened to be broken. "Connectivity: online" in its text
    # output is the actual proof the current context is valid, authenticated,
    # and reachable; anything else (invalid config, connectivity
    # skipped/offline) means it isn't ready.
    _, out, _ = run(["gcx", "config", "check"])
    return "connectivity: online" in out.lower()


def gcx_dependency_status():
    """Status of the `gcx` CLI dependency.

    `blocking` when the CLI isn't installed at all, or is installed but below
    GCX_MIN_VERSION — there'd be no way to use the plugin's skills reliably in
    either case. Installed-and-current-but-not-authenticated is non-blocking:
    the `gcx` plugin has no MCP server of its own (just skills and agents that
    shell out to the `gcx` binary directly when actually invoked), so
    installing it has zero runtime footprint — there's nothing that can be
    left "broken" by installing ahead of authentication, same reasoning
    verified directly for salesforce-prod/uat's MCP server in
    sf_dependency_status."""
    if not gcx_installed():
        return {
            "name": "gcx CLI",
            "installed": False,
            "configured": False,
            "ready": False,
            "detail": "gcx CLI not found on PATH",
            "blocking": True,
        }
    if not gcx_meets_min_version():
        info = gcx_version_info()
        current = info["raw"] if info["parsed"] is None else "v" + ".".join(map(str, info["parsed"]))
        min_str = "v" + ".".join(map(str, GCX_MIN_VERSION))
        return {
            "name": "gcx CLI",
            "installed": True,
            "configured": False,
            "ready": False,
            "detail": (
                f"gcx CLI installed but older than the confirmed-working version ({current}, need "
                f"{min_str}+); run `dep-install gcx` to upgrade (re-running the same install "
                "command overwrites it with the latest release, gcx has no separate self-update)"
            ),
            "blocking": True,
        }
    configured = gcx_context_configured()
    ready = configured and gcx_configured()
    return {
        "name": "gcx CLI",
        "installed": True,
        "configured": configured,
        "ready": ready,
        "detail": (
            "gcx CLI installed and authenticated to a Grafana Cloud stack"
            if ready
            else "gcx CLI installed but not authenticated — run `gcx login --server "
            "https://chg.grafana.net` (or this plugin's own setup-gcx skill) to connect it to "
            "this org's stack"
            if not configured
            else "gcx CLI's current context is set but the session isn't live — run `gcx login "
            "--server https://chg.grafana.net` again to reconnect it"
        ),
        "blocking": False,
    }


def claude_mcp_entry_connected(name_prefix):
    """Live connection health for one of this skill's own MCP registrations —
    logrocket/launch-darkly/atlassian authenticate lazily via OAuth on first
    real tool call, so there's no local CLI to ask ahead of time the way gcx
    or sf have; `claude mcp list`'s own per-entry health probe (read-only, no
    side effects, doesn't itself trigger a login) is the only live signal.
    `name_prefix` is the exact bare MCP name (e.g. "launch-darkly") or a
    "plugin:<plugin_name>:" prefix for plugin-bundled servers (e.g.
    "plugin:logrocket:"). Returns True/False, or None if no matching entry is
    registered at all (shouldn't happen once `installed` is true, but the
    caller should treat None as "unknown" rather than assuming either way)."""
    rc, out, _ = run(["claude", "mcp", "list"])
    if rc != 0:
        return None
    for line in out.splitlines():
        if ": " not in line:
            continue
        name = line.split(": ", 1)[0].strip()
        if name == name_prefix or name.startswith(name_prefix):
            return "✔" in line
    return None


def oauth_session_status(name_prefix):
    """Synthetic non-blocking 'dependency' entry (reusing the same
    dependencies/ready machinery cmd_status already applies to gcx/sf) that
    represents whether a lazily-OAuth'd entry has actually completed its
    browser handshake yet. `blocking: False` — unlike gcx/sf, this never
    blocks `install`, since the MCP entry is already registered regardless;
    it only affects whether the numbered row shows "Installed" plain or
    "Installed — check dependency" per SKILL.md's status mapping."""
    connected = claude_mcp_entry_connected(name_prefix)
    if connected is None:
        return {
            "name": "OAuth session",
            "installed": True,
            "ready": None,
            "detail": "couldn't determine — `claude mcp list` didn't show this entry",
            "blocking": False,
        }
    return {
        "name": "OAuth session",
        "installed": True,
        "ready": connected,
        "detail": (
            "authenticated"
            if connected
            else "registered but not yet authenticated — the first real tool call will "
            "trigger an interactive OAuth prompt (or call one proactively to trigger it now)"
        ),
        "blocking": False,
    }


def claude_org_connector_status(keyword):
    """Detect a pre-existing claude.ai-configured connector whose display name contains
    `keyword` (case-insensitive) — e.g. an Atlassian connector provisioned by the org via
    claude.ai's own Settings > Connectors, entirely separate from anything this skill
    installs. `claude mcp list` has no --json output, so this parses its text lines; any
    entry whose name starts with "plugin:" is one of this skill's own registrations and is
    excluded. Claude Code only — claude.ai connectors don't apply to Copilot CLI."""
    rc, out, _ = run(["claude", "mcp", "list"])
    if rc != 0:
        return None
    for line in out.splitlines():
        if ": " not in line:
            continue
        name = line.split(": ", 1)[0].strip()
        if name.lower().startswith("plugin:"):
            continue
        if keyword.lower() not in name.lower():
            continue
        return {"name": name, "connected": "✔" in line}
    return None


SERVICES = {
    "atlassian": {
        "label": "Atlassian (Jira/Confluence) — full plugin on Claude Code, MCP-only on Copilot CLI",
        "kind": "plugin-or-mcp",
        "marketplace_source": "anthropics/claude-plugins-official",
        "marketplace_name": "claude-plugins-official",
        "plugin_name": "atlassian",
        "mcp_name": "chg-atlassian",
        "mcp_url": "https://mcp.atlassian.com/v1/mcp",
        "oauth_session_match": "plugin:atlassian:",
        "org_connector_check": lambda: claude_org_connector_status("atlassian"),
        "ready_hint": "Authenticates via OAuth automatically on the first real tool call.",
        "post_install": (
            "Authenticates via an interactive OAuth prompt automatically the first time one of "
            "its tools is actually called — nothing to configure ahead of time. After restarting "
            "your session, the first Atlassian request (e.g. 'search Jira for...') will trigger it."
        ),
    },
    "grafana": {
        "label": "Grafana (gcx plugin) — dashboards, alerts, SLOs, incident analysis",
        "kind": "plugin",
        "marketplace_source": "grafana/gcx",
        "marketplace_name": "gcx-marketplace",
        "plugin_name": "gcx",
        "dependencies": lambda: [gcx_dependency_status()],
        "ready_hint": (
            "Registered, but the gcx CLI dependency isn't authenticated right now — run "
            "`gcx login --server https://chg.grafana.net` (or this plugin's own setup-gcx skill) "
            "to reconnect it to this org's stack."
        ),
        "post_install": (
            "install only succeeds once the gcx CLI dependency is already installed and "
            "authenticated (see the dependency check), so this is ready to use as soon as you "
            "restart your session — still required, since the newly installed server isn't "
            "connected in the *current* session. If you ever want to switch which Grafana Cloud "
            "stack it points at, this plugin's own setup-gcx skill can help with that."
        ),
    },
    "launch-darkly": {
        "label": "LaunchDarkly — feature flag management (not used by anything in the pde plugin itself, just handy alongside it)",
        "kind": "mcp",
        "mcp_name": "launch-darkly",
        "mcp_url": "https://mcp.launchdarkly.com/mcp/launchdarkly",
        "oauth_session_match": "launch-darkly",
        "ready_hint": "Authenticates via OAuth automatically on the first real tool call.",
        "post_install": (
            "Authenticates via an interactive OAuth prompt automatically the first time one of "
            "its tools is actually called — nothing to configure ahead of time. After restarting "
            "your session, the first LaunchDarkly request (e.g. 'list my feature flags') will "
            "trigger it."
        ),
    },
    "logrocket": {
        "label": "LogRocket — session replay, metrics, issue search",
        "kind": "plugin",
        "marketplace_source": "logrocket/logrocket-claude-plugin",
        "marketplace_name": "logrocket",
        "plugin_name": "logrocket",
        "oauth_session_match": "plugin:logrocket:",
        "ready_hint": "Authenticates via OAuth automatically on the first real tool call.",
        "post_install": (
            "Authenticates via an interactive OAuth prompt automatically the first time one of "
            "its tools is actually called — nothing to configure ahead of time. After restarting "
            "your session, the first LogRocket request (e.g. 'show me recent LogRocket sessions') "
            "will trigger it."
        ),
    },
    "salesforce-prod": {
        "label": "Salesforce prod — SOQL queries against the prod org",
        "kind": "mcp",
        "mcp_name": "salesforce-prod",
        "mcp_command": ["npx", "-y", "@salesforce/mcp", "--orgs", "prod", "--toolsets", "orgs,data"],
        "dependencies": lambda: [sf_dependency_status("prod")],
        "org_alias": "prod",
        "ready_hint": _sf_service_hints("prod")[0],
        "post_install": _sf_service_hints("prod")[1],
    },
    "salesforce-uat": {
        "label": "Salesforce UAT — SOQL queries against the UAT org",
        "kind": "mcp",
        "mcp_name": "salesforce-uat",
        "mcp_command": ["npx", "-y", "@salesforce/mcp", "--orgs", "uat", "--toolsets", "orgs,data"],
        "dependencies": lambda: [sf_dependency_status("uat")],
        "org_alias": "uat",
        "ready_hint": _sf_service_hints("uat")[0],
        "post_install": _sf_service_hints("uat")[1],
    },
}


def claude_plugin_installed(plugin_name):
    """Match on the plugin name alone (the part of `id` before "@"), not the
    full name@marketplace id — a plugin installed from a differently-named or
    re-added marketplace should still count as installed. Also require
    `enabled` (defaulting true if the field is ever absent), since a disabled
    plugin's MCP server won't actually be reachable.

    Same cwd-sensitivity as find_plugin_root() above applies here for any
    project-scoped install of atlassian/grafana/logrocket — this must be run
    without the caller having `cd`-ed away from the user's real project dir."""
    rc, out, _ = run(["claude", "plugin", "list", "--json"])
    if rc != 0:
        return False
    try:
        data = json.loads(out)
    except Exception:
        return False
    return any(
        p.get("id", "").split("@")[0] == plugin_name and p.get("enabled", True)
        for p in data
    )


def claude_mcp_registered(name):
    config_path = os.path.join(
        os.environ.get("CLAUDE_CONFIG_DIR", os.path.expanduser("~")), ".claude.json"
    )
    try:
        with open(config_path) as f:
            d = json.load(f)
    except Exception:
        return False
    return name in d.get("mcpServers", {})


def copilot_plugin_installed(marketplace_name, plugin_name):
    home = os.environ.get("COPILOT_HOME", os.path.expanduser("~/.copilot"))
    return os.path.isdir(os.path.join(home, "installed-plugins", marketplace_name, plugin_name))


def copilot_mcp_registered(name):
    rc, out, _ = run(["copilot", "mcp", "list", "--json"])
    if rc != 0:
        return False
    try:
        d = json.loads(out)
    except Exception:
        return False
    return name in d.get("mcpServers", {})


def is_installed(service_key, cli):
    svc = SERVICES[service_key]
    if svc["kind"] == "plugin":
        if cli == "claude":
            return claude_plugin_installed(svc["plugin_name"])
        return copilot_plugin_installed(svc["marketplace_name"], svc["plugin_name"])
    if svc["kind"] == "plugin-or-mcp":
        if cli == "claude":
            return claude_plugin_installed(svc["plugin_name"])
        return copilot_mcp_registered(svc["mcp_name"])
    if svc["kind"] == "mcp":
        if cli == "claude":
            return claude_mcp_registered(svc["mcp_name"])
        return copilot_mcp_registered(svc["mcp_name"])
    return False


def cmd_status(cli):
    result = {"pde_mcp": pde_mcp_status(cli)}
    for key, svc in SERVICES.items():
        installed = is_installed(key, cli)
        dependencies = svc["dependencies"]() if "dependencies" in svc else []
        if installed and cli == "claude" and svc.get("oauth_session_match"):
            dependencies = dependencies + [oauth_session_status(svc["oauth_session_match"])]

        ready = None
        if installed:
            dep_ready_values = [d["ready"] for d in dependencies if d["ready"] is not None]
            if dep_ready_values:
                ready = all(dep_ready_values)

        # Unlike `ready`, `configured` isn't gated on `installed` — it's purely a property of the
        # local CLI dependency (sf/gcx), which can already be logged in before the MCP/plugin is
        # ever registered. Services with no such dependency (the synthetic OAuth-session entry
        # included, which has no "configured" key at all) leave this None — rendered as "—", not
        # applicable, in the status table.
        dep_configured_values = [d.get("configured") for d in dependencies if d.get("configured") is not None]
        configured = all(dep_configured_values) if dep_configured_values else None

        entry = {"label": svc["label"], "installed": installed, "configured": configured, "ready": ready}
        if installed and ready is not True and svc.get("ready_hint"):
            entry["note"] = svc["ready_hint"]
        if dependencies:
            entry["dependencies"] = dependencies
        org_check = svc.get("org_connector_check")
        if org_check and cli == "claude":
            org_connector = org_check()
            if org_connector:
                entry["org_connector"] = org_connector
        result[key] = entry
    print(json.dumps(result, indent=2))


def cmd_install(service_key, cli):
    svc = SERVICES[service_key]

    deps_fn = svc.get("dependencies")
    if deps_fn:
        unmet = [d for d in deps_fn() if d.get("blocking") and not d.get("ready")]
        if unmet:
            print(json.dumps({
                "success": False,
                "blocked": True,
                "unmet_dependencies": unmet,
                "error": (
                    "Can't install yet — " +
                    "; ".join(f"{d['name']}: {d['detail']}" for d in unmet) +
                    ". Run `dep-guidance <dependency>` for how to fix this, then try installing "
                    "again."
                ),
            }, indent=2))
            return

    if svc["kind"] == "plugin" or (svc["kind"] == "plugin-or-mcp" and cli == "claude"):
        rc1, o1, e1 = run(
            [cli, "plugin", "marketplace", "add", svc["marketplace_source"]], timeout=120
        )
        if rc1 != 0:
            print(json.dumps({"success": False, "step": "marketplace add", "error": (e1 or o1).strip()}))
            return
        plugin_id = f"{svc['plugin_name']}@{svc['marketplace_name']}"
        rc2, o2, e2 = run([cli, "plugin", "install", plugin_id], timeout=120)
        if rc2 != 0:
            print(json.dumps({"success": False, "step": "plugin install", "error": (e2 or o2).strip()}))
            return
        print(json.dumps({
            "success": True,
            "installed": plugin_id,
            "post_install": svc.get("post_install"),
        }))
        return

    if svc["kind"] == "plugin-or-mcp" and cli == "copilot":
        rc, o, e = run(
            ["copilot", "mcp", "add", svc["mcp_name"], "--transport", "http", svc["mcp_url"]]
        )
        note = "MCP-only — no bundled skills (Copilot CLI has no compatible Atlassian plugin path right now)" if rc == 0 else None
        print(json.dumps({
            "success": rc == 0,
            "installed": svc["mcp_name"] if rc == 0 else None,
            "note": note,
            "post_install": svc.get("post_install") if rc == 0 else None,
            "error": None if rc == 0 else (e or o).strip(),
        }))
        return

    if svc["kind"] == "mcp":
        if "mcp_url" in svc:
            # Remote HTTP server (e.g. launch-darkly) — no command/args, just a URL.
            cmd = [cli, "mcp", "add", "--transport", "http", svc["mcp_name"], svc["mcp_url"]]
            if cli == "claude":
                cmd.insert(3, "--scope")
                cmd.insert(4, "user")
        else:
            # Local/stdio server (e.g. salesforce-prod).
            cmd = [cli, "mcp", "add", svc["mcp_name"]]
            if cli == "claude":
                cmd += ["--scope", "user"]
            cmd += ["--"] + svc["mcp_command"]
        rc, o, e = run(cmd)
        print(json.dumps({
            "success": rc == 0,
            "installed": svc["mcp_name"] if rc == 0 else None,
            "post_install": svc.get("post_install") if rc == 0 else None,
            "error": None if rc == 0 else (e or o).strip(),
        }))
        return


def npm_prefix_writable():
    rc, out, _ = run(["npm", "config", "get", "prefix"])
    if rc != 0:
        return None, None
    prefix = out.strip()
    if not prefix:
        return None, None
    return prefix, os.access(prefix, os.W_OK)


def cmd_dep_guidance(dependency):
    if dependency == "sf":
        print(json.dumps(_resolve_sf_guidance(), indent=2))
    elif dependency == "gcx":
        print(json.dumps(_resolve_gcx_guidance(), indent=2))
    else:
        print(json.dumps({"error": f"no guidance available for dependency '{dependency}'"}))


def cmd_dep_install(dependency):
    if dependency == "sf":
        _dep_install_sf()
    elif dependency == "gcx":
        _dep_install_gcx()
    else:
        print(json.dumps({"success": False, "error": f"no auto-install available for dependency '{dependency}'"}))


def _resolve_sf_guidance():
    """Figures out what needs to happen to get a working `sf` on this machine
    — a fresh install if it's missing, or an upgrade if it's present but below
    SF_MIN_VERSION — and whether that needs root, without actually running
    anything. Shared by `dep-guidance` (just prints this) and `dep-install`
    (acts on it)."""
    system = platform.system()
    action = "upgrade" if sf_installed() and not sf_meets_min_version() else "install"

    version_note = None
    if action == "upgrade":
        info = sf_version_info()
        current = info["raw"] if info["parsed"] is None else "v" + ".".join(map(str, info["parsed"]))
        version_note = (
            f"Currently installed: {current} — below the minimum this MCP server needs "
            f"(v{'.'.join(map(str, SF_MIN_VERSION))}+, the unified CLI)."
        )
        if sf_installed_via_brew():
            # Already came from the cask — upgrading through brew is the only path that
            # won't leave a second, unmanaged copy fighting the brew-owned one on PATH.
            return {
                "dependency": "sf",
                "system": system,
                "action": action,
                "root_required": False,
                "command": "brew upgrade --cask salesforce-cli",
                "reason": "Installed via the Homebrew cask — brew owns it, and brew never needs root.",
                "version_note": version_note,
            }

    rc, _, _ = run(["node", "--version"])
    if rc != 0:
        return {
            "dependency": "sf",
            "system": system,
            "action": action,
            "root_required": None,
            "command": None,
            "reason": "Node.js/npm isn't on PATH, so the sf CLI can't be installed via npm yet.",
            "prerequisite": (
                "Install Node.js first (nodejs.org, or your OS package manager/nvm), then "
                "re-run this check."
            ),
            "version_note": version_note,
        }

    prefix, writable = npm_prefix_writable()
    if writable is None:
        # npm itself missing/broken despite node being present — fall back to a
        # conservative per-OS default rather than guessing wrong.
        writable = system == "Darwin"

    npm_pkg = "@salesforce/cli" if action == "install" else "@salesforce/cli@latest"

    if writable:
        result = {
            "dependency": "sf",
            "system": system,
            "action": action,
            "root_required": False,
            "command": f"npm install -g {npm_pkg}",
            "reason": f"npm's global prefix ({prefix}) is writable by your user — no sudo needed.",
        }
    else:
        # npm's default global prefix being root-owned does NOT mean root is actually required —
        # confirmed by actually running this fix end-to-end on an apt-Node/Ubuntu machine where
        # the default prefix (/usr/local) was root-owned: pointing npm's global prefix at a
        # user-owned directory instead installs cleanly with zero sudo. This is npm's own
        # documented fix (docs.npmjs.com/resolving-eacces-permissions-errors-when-installing-
        # packages-globally), not a workaround specific to this skill — so root_required is
        # False here too, same as the writable-prefix branch above.
        result = {
            "dependency": "sf",
            "system": system,
            "action": action,
            "root_required": False,
            "command": f"npm config set prefix {NPM_USER_PREFIX} && npm install -g {npm_pkg}",
            "reason": (
                f"npm's default global prefix ({prefix}) isn't writable by your user (common when "
                "Node.js came from apt/a system package manager or a shared machine image) — but "
                f"that's not a root requirement: switching npm's global prefix to a directory you "
                f"own ({NPM_USER_PREFIX}) avoids it entirely. npm's own documented fix, not sudo."
            ),
            "note": (
                f"After this, `sf` installs to {NPM_USER_PREFIX}/bin — add "
                f"`export PATH=\"$HOME/.npm-global/bin:$PATH\"` to your shell profile if `sf "
                "--version` isn't found right away."
            ),
        }

    if version_note:
        result["version_note"] = version_note
    if system == "Darwin" and action == "install" and shutil.which("brew"):
        # Confirmed via Salesforce's own Homebrew cask (formulae.brew.sh/cask/salesforce-cli,
        # née the "sf" token) — installs standalone, never touches npm's global prefix at all, so
        # it's an even simpler no-root option than the npm-prefix fallback above. Surface it as an
        # alternative, same pattern as gcx's brew fallback. Only offered for a fresh install —
        # offering it during an upgrade when sf *wasn't* already brew-installed (handled
        # separately above) would create a second, unmanaged copy.
        result["alternative"] = (
            "brew install --cask salesforce-cli (macOS only, no root ever — a single command "
            "instead of reconfiguring npm; run `brew upgrade --cask salesforce-cli` later to "
            "update it)."
        )
        result["alternative_command"] = ["brew", "install", "--cask", "salesforce-cli"]

    return result


def _run_cmd_string(command, timeout=180):
    """Split-and-run a plain command string built by _resolve_*_guidance() —
    every one of those is a static, hardcoded template (no user input
    interpolated into it beyond fixed package/cask names), so a plain arg
    split is safe here."""
    return run(command.split(), timeout=timeout)


def _dep_install_sf():
    guidance = _resolve_sf_guidance()

    if guidance["root_required"] is None:
        print(json.dumps({
            "success": False,
            "blocked": True,
            "dependency": "sf",
            "action": guidance["action"],
            "reason": guidance["reason"],
            "prerequisite": guidance["prerequisite"],
        }, indent=2))
        return

    if guidance.get("alternative_command"):
        rc, out, err = run(guidance["alternative_command"], timeout=180)
        if rc == 0:
            print(json.dumps({
                "success": True,
                "dependency": "sf",
                "action": guidance["action"],
                "method": "brew",
                "command": " ".join(guidance["alternative_command"]),
            }, indent=2))
            return
        # Brew attempt itself failed (e.g. the cask needs an unrelated fix) — fall through to
        # the npm path below rather than silently giving up on a workable route.

    if "npm config set prefix" in guidance["command"]:
        # Compound command (`npm config set prefix ... && npm install -g ...`) — run as discrete
        # steps rather than through a shell, so a failure at either step is unambiguous.
        user_prefix = os.path.expanduser(NPM_USER_PREFIX)
        rc, out, err = run(["npm", "config", "set", "prefix", user_prefix])
        if rc == 0:
            npm_pkg = "@salesforce/cli" if guidance["action"] == "install" else "@salesforce/cli@latest"
            rc, out, err = run(["npm", "install", "-g", npm_pkg], timeout=180)
        print(json.dumps({
            "success": rc == 0,
            "dependency": "sf",
            "action": guidance["action"],
            "method": "npm (user-owned prefix)",
            "command": guidance["command"],
            "note": guidance.get("note"),
            "error": None if rc == 0 else (err or out).strip(),
        }, indent=2))
        return

    rc, out, err = _run_cmd_string(guidance["command"])
    print(json.dumps({
        "success": rc == 0,
        "dependency": "sf",
        "action": guidance["action"],
        "method": "brew" if guidance["command"].startswith("brew") else "npm",
        "command": guidance["command"],
        "error": None if rc == 0 else (err or out).strip(),
    }, indent=2))


def _resolve_gcx_guidance():
    # Sourced from https://github.com/grafana/gcx (docs/installation.md): the
    # official install script defaults to ~/.local/bin, never needs root.
    system = platform.system()
    action = "upgrade" if gcx_installed() and not gcx_meets_min_version() else "install"

    version_note = None
    if action == "upgrade":
        info = gcx_version_info()
        current = info["raw"] if info["parsed"] is None else "v" + ".".join(map(str, info["parsed"]))
        version_note = (
            f"Currently installed: {current} — below the confirmed-working minimum this plugin "
            f"needs (v{'.'.join(map(str, GCX_MIN_VERSION))}+). Re-running the same install command "
            "overwrites it with the latest release — gcx has no separate self-update subcommand."
        )

    if system in ("Linux", "Darwin"):
        result = {
            "dependency": "gcx",
            "system": system,
            "action": action,
            "root_required": False,
            "command": "curl -fsSL https://raw.githubusercontent.com/grafana/gcx/main/scripts/install.sh | sh",
            "reason": "Installs to ~/.local/bin by default — no root needed.",
            "note": (
                "Make sure ~/.local/bin is on PATH afterward — add "
                "`export PATH=\"$HOME/.local/bin:$PATH\"` to your shell profile if `gcx "
                "--version` isn't found right after installing."
            ),
        }
        if version_note:
            result["version_note"] = version_note
        if shutil.which("brew"):
            result["alternative"] = "brew install grafana/grafana/gcx (macOS/Linux, also no root)."
            result["alternative_command"] = ["brew", "install", "grafana/grafana/gcx"]
        return result

    # Windows: no official install script exists for gcx.
    rc, _, _ = run(["go", "version"])
    if rc == 0:
        result = {
            "dependency": "gcx",
            "system": system,
            "action": action,
            "root_required": False,
            "command": "go install github.com/grafana/gcx/cmd/gcx@latest",
            "reason": (
                "No official Windows install script for gcx, but Go is already usable on this "
                "machine — this needs Go 1.24+ and git, which this check confirms are present."
            ),
            "note": (
                "Installs to your Go bin directory (usually %USERPROFILE%\\go\\bin) — make sure "
                "that's on PATH."
            ),
        }
        if version_note:
            result["version_note"] = version_note
        return result

    return {
        "dependency": "gcx",
        "system": system,
        "action": action,
        "root_required": None,
        "command": None,
        "reason": "No official Windows install script for gcx, and Go isn't on PATH either.",
        "prerequisite": (
            "Install Go (go.dev) and git, then run "
            "`go install github.com/grafana/gcx/cmd/gcx@latest` — or download a prebuilt binary "
            "from https://github.com/grafana/gcx/releases and add it to PATH manually."
        ),
    }


def _dep_install_gcx():
    guidance = _resolve_gcx_guidance()

    if guidance["root_required"] is None:
        print(json.dumps({
            "success": False,
            "blocked": True,
            "dependency": "gcx",
            "action": guidance["action"],
            "reason": guidance["reason"],
            "prerequisite": guidance["prerequisite"],
        }, indent=2))
        return

    if guidance.get("alternative_command"):
        rc, out, err = run(guidance["alternative_command"], timeout=180)
        if rc == 0:
            print(json.dumps({
                "success": True,
                "dependency": "gcx",
                "action": guidance["action"],
                "method": "brew",
                "command": " ".join(guidance["alternative_command"]),
            }, indent=2))
            return
        # Brew attempt itself failed — fall through to the official install script below rather
        # than silently giving up on a workable route.

    # root_required is always False for gcx once we get here (curl script installs to
    # ~/.local/bin, go install to the user's Go bin dir) — no sudo path exists to fall back to.
    system = platform.system()
    if system in ("Linux", "Darwin"):
        rc, out, err = run(["sh", "-c", guidance["command"]], timeout=180)
        method = "curl"
    else:
        rc, out, err = _run_cmd_string(guidance["command"])
        method = "go"

    print(json.dumps({
        "success": rc == 0,
        "dependency": "gcx",
        "action": guidance["action"],
        "method": method,
        "command": guidance["command"],
        "note": guidance.get("note"),
        "version_note": guidance.get("version_note"),
        "error": None if rc == 0 else (err or out).strip(),
    }, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_status = sub.add_parser("status")
    p_status.add_argument("--cli", choices=["claude", "copilot"], required=True)

    p_install = sub.add_parser("install")
    p_install.add_argument("service", choices=list(SERVICES.keys()))
    p_install.add_argument("--cli", choices=["claude", "copilot"], required=True)

    p_dep = sub.add_parser("dep-guidance")
    p_dep.add_argument("dependency")

    p_dep_install = sub.add_parser("dep-install")
    p_dep_install.add_argument("dependency")

    args = parser.parse_args()
    if args.cmd == "status":
        cmd_status(args.cli)
    elif args.cmd == "install":
        cmd_install(args.service, args.cli)
    elif args.cmd == "dep-guidance":
        cmd_dep_guidance(args.dependency)
    elif args.cmd == "dep-install":
        cmd_dep_install(args.dependency)


if __name__ == "__main__":
    main()
