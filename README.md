# provider-hub

A shared team repository for AI skills, MCP configs, APIs, scripts, services, tools, and more.

## Purpose

`provider-hub` is a central location for team-shared and individual contributions:
- **AI Skills** — Copilot and autonomous agent extensions
- **MCP Servers** — Model Context Protocol server configs
- **APIs** — OpenAPI specs, SDKs, integrations
- **Scripts** — Automation and CLI tools
- **Services** — Deployable applications (schedulers, agents, REST APIs)
- **Tools** — Reusable utilities and libraries

## Quick Start

```bash
# Clone the repo
git clone https://github.com/chg-ncanen/provider-hub.git
cd provider-hub

# Explore available content
ls -la ai-skills/team/pde/     # Team PDE skills
ls -la scripts/user/ncanen/    # User scripts
```

## Repository Structure

```
provider-hub/
├── ai-skills/       Team & user AI skills
├── apis/            API specs and integrations
├── mcp/             MCP server configs
├── scripts/         Automation scripts
├── services/        Deployable services
├── tools/           Reusable utilities
├── plugins/         Claude Code plugins (see "Using skills & MCPs elsewhere" below)
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

## Using skills & MCPs from another project

Most teammates won't have `provider-hub` as their working directory, so a skill or MCP server living
under `ai-skills/`/`mcp/` alone isn't reachable from elsewhere — those paths are for authoring and
review. Anything meant to be *used* outside this repo ships as a **plugin** under `plugins/<team>/`,
listed in `.claude-plugin/marketplace.json`. Plugins register globally on install, independent of
cwd — and the same manifest works for both Claude Code and GitHub Copilot CLI, which both recognize
the `.claude-plugin/` layout:

```bash
# Claude Code
/plugin marketplace add https://github.com/chg-ncanen/provider-hub.git
/plugin install pde@provider-hub

# Copilot CLI
copilot plugin marketplace add https://github.com/chg-ncanen/provider-hub.git
copilot plugin install pde@provider-hub
```

The plugin's own `README.md` documents what it bundles and any required credentials/prerequisites.
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

## Skill Discovery & Configuration

### Local Development

**Option 1: Environment Variable** (simplest)
```bash
export COPILOT_SKILLS_PATH="$HOME/dev/provider-hub/ai-skills/team:$HOME/dev/provider-hub/ai-skills/user"
copilot-cli  # Will discover skills from those paths
```

**Option 2: Git Submodule** (stronger version control)
In your working project's `.git/config`:
```ini
[submodule "provider-hub"]
    path = .agents/provider-hub
    url = /path/to/provider-hub
```

Then in `.agents/mcp-config.json`:
```json
{
  "skills_paths": [
    ".agents/provider-hub/ai-skills/team",
    ".agents/provider-hub/ai-skills/user"
  ]
}
```

### MCP-Based Discovery (Team)

Skills can also be discovered via an MCP server endpoint:
```json
{
  "mcpServers": {
    "provider-hub-skills": {
      "command": "python",
      "args": ["/path/to/mcp/provider-hub-skills.py"]
    }
  }
}
```

See `mcp/README.md` for details.

## Governance

- **CODEOWNERS** enforces review requirements (see `.github/CODEOWNERS`)
- **Team areas** require team member approval
- **User areas** are self-reviewed but should follow conventions
- **Services** must include deployment docs and Dockerfile

## Naming & Conventions

- **Skills:** Prefix with team/area (e.g., `pde-ai-ticket-discovery`)
- **Services:** Clear, descriptive name (e.g., `agent-scheduler`, `pde-jsm-mcp`)
- **Scripts:** Lowercase with hyphens (e.g., `sync-contacts`, `deploy-worker`)
- **Directories:** Lowercase with hyphens (no spaces or special chars)

## Support

- Questions? Check the README in the relevant content-type directory
- Issues? Open a GitHub issue (TBD: link when repo goes public)
- PRs welcome! Follow [CONTRIBUTING.md](CONTRIBUTING.md)

---

**Last updated:** Repository initialization
