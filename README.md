# provider-hub

A shared team repository for AI skills, MCP configs, APIs, scripts, services, tools, and more.

> **Note:** Repository name subject to change. Final name TBD before pushing to GitHub.

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
git clone https://path/to/provider-hub.git
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
│
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
