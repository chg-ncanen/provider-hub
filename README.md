# provider-hub

## Overview

`provider-hub` is a shared team repository for AI skills, MCP server configs, APIs, scripts,
services, and tools — a central location for team-shared and individual contributions:
- **AI Skills** — Copilot and autonomous agent extensions
- **MCP Servers** — Model Context Protocol server configs
- **APIs** — OpenAPI specs, SDKs, integrations
- **Scripts** — Automation and CLI tools
- **Services** — Deployable applications (schedulers, agents, REST APIs)
- **Tools** — Reusable utilities and libraries

Most of this content (`ai-skills/`, `mcp/`, `apis/`, `scripts/`, `services/`, `tools/`) is organized
for authoring and review, and isn't reachable from outside this repo. Anything meant to be
*installed and used* from any other project — a skill plus the MCP server(s) it needs — ships as a
**plugin** under `plugins/`, installable via Claude Code or GitHub Copilot CLI regardless of your
working directory (see [Installing & Using](#installing--using) below).

Right now the only working example is the `pde` plugin (JSM alert management + the
`resolve-duplicate-contact-alerts` skill). Everything else is scaffolding awaiting contributions —
see [Repository Structure](#repository-structure).

## Installing & Using

First, add this repo as a marketplace:

```bash
# Claude Code
/plugin marketplace add https://github.com/chg-ncanen/provider-hub.git

# Copilot CLI
copilot plugin marketplace add https://github.com/chg-ncanen/provider-hub.git
```

Then install a plugin — either by typing the install command directly, or by browsing what the
marketplace has first:

```bash
# Claude Code — direct install
/plugin install pde@provider-hub

# Claude Code — browse instead: run /plugin with no arguments, open the "Discover" tab
# (lists plugins from every marketplace you've added, provider-hub included), and select
# pde from there. Functionally identical to the direct command above.

# Copilot CLI — direct install
copilot plugin install pde@provider-hub

# Copilot CLI — browse instead:
copilot plugin marketplace browse provider-hub
```

Plugins register globally on install, independent of your working directory — the same
`.claude-plugin/` manifest works for both CLIs, since both recognize that layout.

**Then start a new session** (close and reopen) before using it — installing alone isn't enough for
plugins with a dependency-setup hook. See `plugins/pde/README.md` for what it provides, required
credentials, dependency setup (handled automatically), how to pull in optional companion MCPs
(Grafana, LogRocket, Atlassian, Salesforce, LaunchDarkly), and how to actually use it once installed.

As more plugins are added under `plugins/`, install any of them the same way, swapping `pde` for the
plugin's name.

## Repository Structure

```
provider-hub/
├── ai-skills/       Team & user AI skills
├── apis/            API specs and integrations
├── mcp/             MCP server configs
├── scripts/         Automation scripts
├── services/        Deployable services
├── tools/           Reusable utilities
├── plugins/         Installable plugins (see "Installing & Using" above)
│
├── .claude-plugin/
│   └── marketplace.json              # Lists the plugins under plugins/
├── .github/
│   ├── CODEOWNERS                    # Ownership & review rules
│   └── pull_request_template.md      # PR guidelines
│
├── README.md        (this file)
├── CONTRIBUTING.md  Contribution guidelines
└── .gitignore
```

Each content type has:
- `team/` — shared across the team (organized by area: provider, pde, web)
- `user/` — individual contributions (organized by username)

## Packaging something as a plugin

If you're contributing a skill (and the MCP server(s) it needs) that should work from *any* project,
package it as a plugin under `plugins/<team>/`, listed in `.claude-plugin/marketplace.json` — see
`plugins/pde/` for a working example, and [CONTRIBUTING.md](CONTRIBUTING.md) for the full pattern.

`ai-skills/`, `mcp/`, `tools/`, etc. remain the right home for reusable libraries, scripts, services,
and anything that doesn't need standalone distribution — a plugin assembles the pieces it needs from
there (e.g. `plugins/pde/mcp-servers/pde-mcp` depends on `tools/team/pde/pde-ops-api` as a normal pip
dependency, not a copy).

## How to Contribute

1. **Identify the right place:** See [CONTRIBUTING.md](CONTRIBUTING.md) for details
2. **Create your content:** Add files under the appropriate path
3. **Write documentation:** Include a README explaining what it is and how to use it
4. **Submit a PR:** Follow the checklist in the PR template
5. **Get reviewed:** CODEOWNERS will review based on content type and area

## Governance

- **CODEOWNERS** enforces review requirements (see `.github/CODEOWNERS`)
- **Team areas** require team member approval
- **User areas** are self-reviewed but should follow conventions
- **Services** must include deployment docs and Dockerfile

## Naming & Conventions

- **Skills:** Prefix with team/area (e.g., `pde-ai-ticket-discovery`)
- **Services:** Clear, descriptive name (e.g., `agent-scheduler`, `pde-mcp`)
- **Scripts:** Lowercase with hyphens (e.g., `sync-contacts`, `deploy-worker`)
- **Directories:** Lowercase with hyphens (no spaces or special chars)

## Support

- Questions? Check the README in the relevant content-type directory
- Issues? Open a GitHub issue
- PRs welcome! Follow [CONTRIBUTING.md](CONTRIBUTING.md)
