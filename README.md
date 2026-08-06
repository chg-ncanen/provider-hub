# provider-hub

## Overview

`provider-hub` is a shared team repository for two kinds of content:
- **`plugins/`** — installable units (skills + the MCP server(s) they need), installable via Claude
  Code or GitHub Copilot CLI from any project, regardless of your working directory (see
  [Installing & Using](#installing--using) below).
- **`shared/`** — library code that plugins depend on but that isn't itself installable (e.g. a
  Python package pulled in via `requirements.txt`). Not reachable from outside this repo.

Right now the only working example is the `pde-ops-tools` plugin (JSM alert management + the
`resolve-duplicate-contact-alerts` and `dependabot-triage` skills), plus `shared/pde-ops-api`, the
library it depends on. See [Repository Structure](#repository-structure).

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
plugins with a dependency-setup hook. See `plugins/pde-ops-tools/README.md` for what it provides,
required credentials, dependency setup (handled automatically), how to pull in optional companion
MCPs (Grafana, LogRocket, Atlassian, Salesforce, LaunchDarkly), and how to actually use it once
installed.

As more plugins are added under `plugins/`, install any of them the same way, swapping `pde` for
the plugin's name.

## Repository Structure

```
provider-hub/
├── plugins/         Installable plugins (see "Installing & Using" above)
├── shared/          Library code plugins depend on, not itself installable
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

## Packaging something as a plugin

If you're contributing a skill (and the MCP server(s) it needs) that should work from *any* project,
package it as a plugin under `plugins/<name>/`, listed in `.claude-plugin/marketplace.json` — see
`plugins/pde-ops-tools/` for a working example, and [CONTRIBUTING.md](CONTRIBUTING.md) for the full
pattern.

`shared/` remains the right home for library code that doesn't need standalone distribution — a
plugin depends on it as a normal package dependency rather than copying it in (e.g.
`plugins/pde-ops-tools/mcp-servers/pde-mcp` depends on `shared/pde-ops-api` via `requirements.txt`).

## How to Contribute

1. **Identify the right place:** See [CONTRIBUTING.md](CONTRIBUTING.md) for details
2. **Create your content:** Add files under `plugins/` or `shared/`
3. **Write documentation:** Include a README explaining what it is and how to use it
4. **Submit a PR:** Follow the checklist in the PR template
5. **Get reviewed:** CODEOWNERS will review

## Governance

- **CODEOWNERS** enforces review requirements (see `.github/CODEOWNERS`)
- **`plugins/` and `shared/`** are team-owned and require review before merge

## Naming & Conventions

- **Plugins:** `plugin.json`'s `name` field is the install/namespace identity (`pde@provider-hub`,
  `/pde:skill-name`) — keep it short and stable. The folder under `plugins/` can be more descriptive
  (e.g. `plugins/pde-ops-tools/` for the plugin named `pde`) since it's never user-facing.
- **Skills:** Prefix with team/area (e.g., `pde-ai-ticket-discovery`)
- **Directories:** Lowercase with hyphens (no spaces or special chars)

## Support

- Questions? Check the README in `plugins/<name>/` or `shared/<name>/`
- Issues? Open a GitHub issue
- PRs welcome! Follow [CONTRIBUTING.md](CONTRIBUTING.md)
