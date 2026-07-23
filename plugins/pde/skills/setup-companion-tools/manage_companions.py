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
"""
import argparse
import json
import os
import platform
import shutil
import subprocess


def run(cmd, timeout=30):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except Exception as e:
        return 1, "", str(e)


def sf_installed():
    rc, _, _ = run(["sf", "--version"])
    return rc == 0


def sf_aliases():
    rc, out, _ = run(["sf", "alias", "list", "--json"])
    if rc != 0:
        return set()
    try:
        d = json.loads(out)
        return {a.get("alias") for a in d.get("result", [])}
    except Exception:
        return set()


def sf_dependency_status(alias):
    """Status of the `sf` CLI dependency, scoped to one org alias (prod/uat) —
    each Salesforce service needs its own alias logged in, even though the
    CLI binary itself is shared. `blocking: True` — registering the MCP entry
    before the CLI can actually authenticate would just leave a broken-looking
    install sitting there, so `install` refuses until this is ready."""
    if not sf_installed():
        return {
            "name": "sf CLI",
            "installed": False,
            "ready": False,
            "detail": "sf CLI not found on PATH",
            "blocking": True,
        }
    ready = alias in sf_aliases()
    return {
        "name": "sf CLI",
        "installed": True,
        "ready": ready,
        "detail": (
            f"sf CLI installed, logged into '{alias}'"
            if ready
            else f"sf CLI installed but not logged into '{alias}'"
        ),
        "blocking": True,
    }


def gcx_installed():
    rc, _, _ = run(["gcx", "--version"])
    return rc == 0


def gcx_configured():
    # `gcx config check` verifies the current context is actually authenticated
    # and connected to a Grafana Cloud stack, not just that the binary exists.
    rc, _, _ = run(["gcx", "config", "check"])
    return rc == 0


def gcx_dependency_status():
    """Status of the `gcx` CLI dependency. `blocking: True` — the Grafana
    plugin's MCP server shells out to this binary directly (this isn't the
    hosted HTTP Grafana MCP), so registering the plugin before it's present
    would leave the same kind of broken-looking install as sf would for
    salesforce-prod — treat it the same way."""
    if not gcx_installed():
        return {
            "name": "gcx CLI",
            "installed": False,
            "ready": False,
            "detail": "gcx CLI not found on PATH",
            "blocking": True,
        }
    ready = gcx_configured()
    return {
        "name": "gcx CLI",
        "installed": True,
        "ready": ready,
        "detail": (
            "gcx CLI installed and authenticated to a Grafana Cloud stack"
            if ready
            else "gcx CLI installed but not authenticated — run `gcx login` (or this plugin's "
            "own setup-gcx skill) to connect it to a stack"
        ),
        "blocking": True,
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
    "grafana": {
        "label": "Grafana (gcx plugin) — dashboards, alerts, SLOs, incident analysis",
        "kind": "plugin",
        "marketplace_source": "grafana/gcx",
        "marketplace_name": "gcx-marketplace",
        "plugin_name": "gcx",
        "dependencies": lambda: [gcx_dependency_status()],
        "ready_hint": (
            "Registered, but the gcx CLI dependency isn't authenticated right now — run "
            "`gcx login` (or this plugin's own setup-gcx skill) to reconnect it to a stack."
        ),
        "post_install": (
            "install only succeeds once the gcx CLI dependency is already installed and "
            "authenticated (see the dependency check), so this is ready to use as soon as you "
            "restart your session — still required, since the newly installed server isn't "
            "connected in the *current* session. If you ever want to switch which Grafana Cloud "
            "stack it points at, this plugin's own setup-gcx skill can help with that."
        ),
    },
    "logrocket": {
        "label": "LogRocket — session replay, metrics, issue search",
        "kind": "plugin",
        "marketplace_source": "logrocket/logrocket-claude-plugin",
        "marketplace_name": "logrocket",
        "plugin_name": "logrocket",
        "ready_hint": "Authenticates via OAuth automatically on the first real tool call.",
        "post_install": (
            "Authenticates via an interactive OAuth prompt automatically the first time one of "
            "its tools is actually called — nothing to configure ahead of time. After restarting "
            "your session, the first LogRocket request (e.g. 'show me recent LogRocket sessions') "
            "will trigger it."
        ),
    },
    "atlassian": {
        "label": "Atlassian (Jira/Confluence) — full plugin on Claude Code, MCP-only on Copilot CLI",
        "kind": "plugin-or-mcp",
        "marketplace_source": "anthropics/claude-plugins-official",
        "marketplace_name": "claude-plugins-official",
        "plugin_name": "atlassian",
        "mcp_name": "chg-atlassian",
        "mcp_url": "https://mcp.atlassian.com/v1/mcp",
        "org_connector_check": lambda: claude_org_connector_status("atlassian"),
        "ready_hint": "Authenticates via OAuth automatically on the first real tool call.",
        "post_install": (
            "Authenticates via an interactive OAuth prompt automatically the first time one of "
            "its tools is actually called — nothing to configure ahead of time. After restarting "
            "your session, the first Atlassian request (e.g. 'search Jira for...') will trigger it."
        ),
    },
    "salesforce-prod": {
        "label": "Salesforce prod — SOQL queries against the prod org",
        "kind": "mcp",
        "mcp_name": "salesforce-prod",
        "mcp_command": ["npx", "-y", "@salesforce/mcp", "--orgs", "prod", "--toolsets", "orgs,data"],
        "dependencies": lambda: [sf_dependency_status("prod")],
        "org_alias": "prod",
    },
    "salesforce-uat": {
        "label": "Salesforce UAT — SOQL queries against the UAT org",
        "kind": "mcp",
        "mcp_name": "salesforce-uat",
        "mcp_command": ["npx", "-y", "@salesforce/mcp", "--orgs", "uat", "--toolsets", "orgs,data"],
        "dependencies": lambda: [sf_dependency_status("uat")],
        "org_alias": "uat",
    },
    "launch-darkly": {
        "label": "LaunchDarkly — feature flag management (not used by anything in the pde plugin itself, just handy alongside it)",
        "kind": "mcp",
        "mcp_name": "launch-darkly",
        "mcp_url": "https://mcp.launchdarkly.com/mcp/launchdarkly",
        "ready_hint": "Authenticates via OAuth automatically on the first real tool call.",
        "post_install": (
            "Authenticates via an interactive OAuth prompt automatically the first time one of "
            "its tools is actually called — nothing to configure ahead of time. After restarting "
            "your session, the first LaunchDarkly request (e.g. 'list my feature flags') will "
            "trigger it."
        ),
    },
}


def claude_plugin_installed(plugin_name):
    """Match on the plugin name alone (the part of `id` before "@"), not the
    full name@marketplace id — a plugin installed from a differently-named or
    re-added marketplace should still count as installed. Also require
    `enabled` (defaulting true if the field is ever absent), since a disabled
    plugin's MCP server won't actually be reachable."""
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
    result = {}
    for key, svc in SERVICES.items():
        installed = is_installed(key, cli)
        dependencies = svc["dependencies"]() if "dependencies" in svc else []

        ready = None
        if installed:
            dep_ready_values = [d["ready"] for d in dependencies if d["ready"] is not None]
            if dep_ready_values:
                ready = all(dep_ready_values)

        entry = {"label": svc["label"], "installed": installed, "ready": ready}
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
        _dep_guidance_sf()
    elif dependency == "gcx":
        _dep_guidance_gcx()
    else:
        print(json.dumps({"error": f"no guidance available for dependency '{dependency}'"}))


def _dep_guidance_sf():
    system = platform.system()

    rc, _, _ = run(["node", "--version"])
    if rc != 0:
        print(json.dumps({
            "dependency": "sf",
            "system": system,
            "root_required": None,
            "command": None,
            "reason": "Node.js/npm isn't on PATH, so the sf CLI can't be installed via npm yet.",
            "prerequisite": (
                "Install Node.js first (nodejs.org, or your OS package manager/nvm), then "
                "re-run this check."
            ),
        }, indent=2))
        return

    prefix, writable = npm_prefix_writable()
    if writable is None:
        # npm itself missing/broken despite node being present — fall back to a
        # conservative per-OS default rather than guessing wrong.
        writable = system == "Darwin"

    if system == "Windows":
        root_required = not writable
        command = "npm install -g @salesforce/cli"
        reason = (
            "This machine's npm prefix is user-writable — no elevation needed."
            if writable
            else "Right-click your terminal (PowerShell or cmd) and choose 'Run as "
            "Administrator' first, then run this."
        )
    else:
        root_required = not writable
        if writable:
            command = "npm install -g @salesforce/cli"
            reason = f"npm's global prefix ({prefix}) is writable by your user — no sudo needed."
        else:
            command = "sudo npm install -g @salesforce/cli"
            reason = (
                f"npm's global prefix ({prefix}) isn't writable by your user (common when "
                "Node.js came from apt/a system package manager) — this needs root."
            )

    print(json.dumps({
        "dependency": "sf",
        "system": system,
        "root_required": root_required,
        "command": command,
        "reason": reason,
    }, indent=2))


def _dep_guidance_gcx():
    # Sourced from https://github.com/grafana/gcx (docs/installation.md): the
    # official install script defaults to ~/.local/bin, never needs root — a
    # different situation from sf's npm-global install, which often does.
    system = platform.system()

    if system in ("Linux", "Darwin"):
        print(json.dumps({
            "dependency": "gcx",
            "system": system,
            "root_required": False,
            "command": "curl -fsSL https://raw.githubusercontent.com/grafana/gcx/main/scripts/install.sh | sh",
            "reason": "Installs to ~/.local/bin by default — no root needed.",
            "note": (
                "Make sure ~/.local/bin is on PATH afterward — add "
                "`export PATH=\"$HOME/.local/bin:$PATH\"` to your shell profile if `gcx "
                "--version` isn't found right after installing."
            ),
            "alternative": "brew install grafana/grafana/gcx (macOS/Linux, also no root).",
        }, indent=2))
        return

    # Windows: no official install script exists for gcx.
    rc, _, _ = run(["go", "version"])
    if rc == 0:
        print(json.dumps({
            "dependency": "gcx",
            "system": system,
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
        }, indent=2))
        return

    print(json.dumps({
        "dependency": "gcx",
        "system": system,
        "root_required": None,
        "command": None,
        "reason": "No official Windows install script for gcx, and Go isn't on PATH either.",
        "prerequisite": (
            "Install Go (go.dev) and git, then run "
            "`go install github.com/grafana/gcx/cmd/gcx@latest` — or download a prebuilt binary "
            "from https://github.com/grafana/gcx/releases and add it to PATH manually."
        ),
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

    args = parser.parse_args()
    if args.cmd == "status":
        cmd_status(args.cli)
    elif args.cmd == "install":
        cmd_install(args.service, args.cli)
    elif args.cmd == "dep-guidance":
        cmd_dep_guidance(args.dependency)


if __name__ == "__main__":
    main()
