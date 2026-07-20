#!/usr/bin/env python3
"""
Check status of, and install, one at a time, the optional companion
MCPs/plugins commonly used alongside PDE tooling — none of which are
bundled in the `pde` plugin itself (grafana/gcx, logrocket, atlassian,
salesforce-prod, launch-darkly). Driven by the setup-companion-tools
skill; never runs on its own.

Usage:
    python3 manage_companions.py status --cli claude|copilot
    python3 manage_companions.py install <service> --cli claude|copilot
    python3 manage_companions.py sf-cli-guidance
"""
import argparse
import json
import os
import platform
import subprocess

SERVICES = {
    "grafana": {
        "label": "Grafana (gcx plugin) — dashboards, alerts, SLOs, incident analysis",
        "kind": "plugin",
        "marketplace_source": "grafana/gcx",
        "marketplace_name": "gcx-marketplace",
        "plugin_name": "gcx",
    },
    "logrocket": {
        "label": "LogRocket — session replay, metrics, issue search",
        "kind": "plugin",
        "marketplace_source": "logrocket/logrocket-claude-plugin",
        "marketplace_name": "logrocket",
        "plugin_name": "logrocket",
    },
    "atlassian": {
        "label": "Atlassian (Jira/Confluence) — full plugin on Claude Code, MCP-only on Copilot CLI",
        "kind": "plugin-or-mcp",
        "marketplace_source": "anthropics/claude-plugins-official",
        "marketplace_name": "claude-plugins-official",
        "plugin_name": "atlassian",
        "mcp_name": "chg-atlassian",
        "mcp_url": "https://mcp.atlassian.com/v1/mcp",
    },
    "salesforce-prod": {
        "label": "Salesforce prod — SOQL queries against the prod org (needed by resolve-duplicate-contact-alerts)",
        "kind": "mcp",
        "mcp_name": "salesforce-prod",
        "mcp_command": ["npx", "-y", "@salesforce/mcp", "--orgs", "prod", "--toolsets", "orgs,data"],
        "needs_sf_cli": True,
    },
    "launch-darkly": {
        "label": "LaunchDarkly — feature flag management (not used by anything in the pde plugin itself, just handy alongside it)",
        "kind": "mcp",
        "mcp_name": "launch-darkly",
        "mcp_url": "https://mcp.launchdarkly.com/mcp/launchdarkly",
    },
}


def run(cmd, timeout=30):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except Exception as e:
        return 1, "", str(e)


def claude_plugin_installed(plugin_id):
    rc, out, _ = run(["claude", "plugin", "list", "--json"])
    if rc != 0:
        return False
    try:
        data = json.loads(out)
    except Exception:
        return False
    return any(p.get("id") == plugin_id for p in data)


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
            return claude_plugin_installed(f"{svc['plugin_name']}@{svc['marketplace_name']}")
        return copilot_plugin_installed(svc["marketplace_name"], svc["plugin_name"])
    if svc["kind"] == "plugin-or-mcp":
        if cli == "claude":
            return claude_plugin_installed(f"{svc['plugin_name']}@{svc['marketplace_name']}")
        return copilot_mcp_registered(svc["mcp_name"])
    if svc["kind"] == "mcp":
        if cli == "claude":
            return claude_mcp_registered(svc["mcp_name"])
        return copilot_mcp_registered(svc["mcp_name"])
    return False


def sf_cli_status():
    rc, _, _ = run(["sf", "--version"])
    if rc != 0:
        return {"installed": False, "prod_alias": False}
    prod = False
    rc, out, _ = run(["sf", "alias", "list", "--json"])
    if rc == 0:
        try:
            d = json.loads(out)
            prod = any(a.get("alias") == "prod" for a in d.get("result", []))
        except Exception:
            pass
    return {"installed": True, "prod_alias": prod}


def cmd_status(cli):
    result = {k: {"label": v["label"], "installed": is_installed(k, cli)} for k, v in SERVICES.items()}
    result["_sf_cli"] = sf_cli_status()
    print(json.dumps(result, indent=2))


def cmd_install(service_key, cli):
    svc = SERVICES[service_key]

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
        print(json.dumps({"success": True, "installed": plugin_id}))
        return

    if svc["kind"] == "plugin-or-mcp" and cli == "copilot":
        rc, o, e = run(
            ["copilot", "mcp", "add", svc["mcp_name"], "--transport", "http", svc["mcp_url"]]
        )
        print(json.dumps({
            "success": rc == 0,
            "installed": svc["mcp_name"] if rc == 0 else None,
            "note": "MCP-only — no bundled skills (Copilot CLI has no compatible Atlassian plugin path right now)" if rc == 0 else None,
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
            "error": None if rc == 0 else (e or o).strip(),
        }))
        return


def cmd_sf_cli_guidance():
    system = platform.system()
    guidance = {
        "Linux": {
            "note": "Check /etc/os-release if you need to confirm Ubuntu/Debian vs. another distro — the advice below applies broadly to any apt- or system-package-installed Node.js.",
            "no_sudo_path": "npm config set prefix ~/.npm-global && export PATH=$HOME/.npm-global/bin:$PATH (add that export to ~/.bashrc or ~/.zshrc), then: npm install -g @salesforce/cli",
            "with_sudo_path": "sudo npm install -g @salesforce/cli — needed if npm's global prefix is root-owned, which is common when Node.js came from apt/the system package manager.",
        },
        "Darwin": {
            "note": "If Node.js was installed via Homebrew (brew install node), npm's global prefix is already user-owned.",
            "no_sudo_path": "npm install -g @salesforce/cli — should just work without sudo on Homebrew-managed Node.",
            "with_sudo_path": "sudo npm install -g @salesforce/cli, or switch to a Homebrew-managed Node.js to avoid needing sudo at all.",
        },
        "Windows": {
            "note": "Behavior depends on how Node.js was installed (official installer, nvm-windows, winget, etc.).",
            "no_sudo_path": "npm install -g @salesforce/cli — try this first from a normal terminal.",
            "with_sudo_path": "Right-click your terminal (PowerShell or cmd) and choose 'Run as Administrator', then: npm install -g @salesforce/cli",
        },
    }
    print(json.dumps(
        {"system": system, "guidance": guidance.get(system, guidance["Linux"])},
        indent=2,
    ))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_status = sub.add_parser("status")
    p_status.add_argument("--cli", choices=["claude", "copilot"], required=True)

    p_install = sub.add_parser("install")
    p_install.add_argument("service", choices=list(SERVICES.keys()))
    p_install.add_argument("--cli", choices=["claude", "copilot"], required=True)

    sub.add_parser("sf-cli-guidance")

    args = parser.parse_args()
    if args.cmd == "status":
        cmd_status(args.cli)
    elif args.cmd == "install":
        cmd_install(args.service, args.cli)
    elif args.cmd == "sf-cli-guidance":
        cmd_sf_cli_guidance()


if __name__ == "__main__":
    main()
