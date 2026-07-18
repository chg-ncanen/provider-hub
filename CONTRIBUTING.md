# Contributing to provider-hub

Thank you for contributing! This guide explains where things go and how the review process works.

## Content Types & Where They Live

### AI Skills
**Directory:** `ai-skills/team/<area>/` or `ai-skills/user/<username>/`

AI skills extend Copilot and autonomous agents. Each skill needs:
- `SKILL.md` — Metadata file (name, description, capabilities)
- Supporting files (Python scripts, configs, data)
- `README.md` — Usage and integration guide

**Naming:** Prefix with team/area and function, e.g., `pde-ai-ticket-discovery`

**Example:**
```
ai-skills/team/pde/
  pde-ai-ticket-discovery/
    ├── SKILL.md
    ├── discovery.py
    └── README.md
```

### APIs
**Directory:** `apis/team/<area>/` or `apis/user/<username>/`

Share API specs, SDKs, and integrations.

**Include:**
- OpenAPI spec (YAML or JSON)
- SDK examples (if applicable)
- Integration guide
- `README.md`

### MCP Servers
**Directory:** `mcp/team/<area>/` or `mcp/user/<username>/`

Model Context Protocol server configs and definitions.

**Include:**
- MCP server config/definition
- Tools exposed by the server
- Setup instructions
- `README.md`

**If this needs to be usable outside this repo** (most team MCP servers/skills do — see
[Distributing as a plugin](#distributing-as-a-plugin) below), the actual server code and the skill(s)
that use it move into `plugins/<area>/` instead of staying split across `ai-skills/`/`mcp/`. Shared
library code it depends on (`tools/team/<area>/...`) stays put and becomes a normal pip/package
dependency — don't duplicate that source into the plugin.

### Scripts
**Directory:** `scripts/team/<area>/` or `scripts/user/<username>/`

Automation scripts and CLI tools (local use only, not deployed).

**Include:**
- Script files (Python, Bash, Node, etc.)
- `requirements.txt` / `package.json` / dependencies
- Usage examples
- `README.md`

### Services
**Directory:** `services/team/<area>/` or `services/user/<username>/`

Deployable services: REST APIs, agent schedulers, MCP servers, autonomous agents.

**Include:**
- Source code (`src/`, `main.py`, etc.)
- `Dockerfile` — required for all services
- `docker-compose.yml` (optional, for local dev)
- Kubernetes manifests or infra files (if applicable)
- Deployment guide in `README.md`

**Example:**
```
services/team/pde/agent-scheduler/
  ├── src/
  │   ├── __init__.py
  │   └── scheduler.py
  ├── Dockerfile
  ├── docker-compose.yml
  ├── requirements.txt
  └── README.md (deployment instructions)
```

### Tools
**Directory:** `tools/team/<area>/` or `tools/user/<username>/`

Reusable utilities and libraries (local use, not deployed).

**Include:**
- Source files
- Dependency specs
- Documentation
- `README.md`

## Distributing as a plugin

`ai-skills/`, `mcp/`, and `tools/` are organized by content type for authoring and review, but they
aren't reachable from someone else's Claude Code session unless `provider-hub` happens to be their
cwd. If a skill (and the MCP server(s) it needs) should work from *any* project, package it as a
Claude Code plugin:

- Create `plugins/<area>/` with a self-contained `.claude-plugin/plugin.json`, `skills/`, and
  `.mcp.json`/`mcp-servers/` — see `plugins/pde/` for a working example.
- List it in the root `.claude-plugin/marketplace.json`.
- Keep genuinely reusable library code (not glue/wrapper code) under `tools/team/<area>/` as an
  independently pip-installable package, and have the plugin's MCP server depend on it via
  `requirements.txt` instead of copying its source in — a plugin can't reference files outside its
  own directory once installed, so anything it needs must either live inside `plugins/<area>/` or be
  a real package dependency.
- Content that doesn't need standalone distribution (experiments, scripts, services, user-scoped
  tools) has no reason to move — it stays under its existing content-type directory.

Copilot CLI has no plugin system; if your content needs to support Copilot CLI too, also follow the
`mcp/`/`ai-skills/` registration path described in each MCP server's README.

## Ownership Model

### Team Areas (`team/provider/`, `team/pde/`, `team/web/`)
- Shared across the team
- Require review from CODEOWNERS before merge
- Should be well-documented and maintained
- Best for widely-used, stable code

### User Areas (`user/<username>/`)
- Individual contributions
- Self-reviewed (PRs just need author approval)
- Opt-in for team adoption
- Good for experiments and personal tools

## How to Contribute

1. **Create a branch**
   ```bash
   git checkout -b feat/my-skill-name
   ```

2. **Add your content** in the appropriate directory following the structure above

3. **Include documentation**
   - README.md explaining what it is and how to use it
   - For skills: SKILL.md with metadata
   - For services: deployment guide

4. **No secrets!**
   - Never commit credentials, API keys, or sensitive data
   - Use environment variables or config files (in .gitignore)

5. **Submit a PR**
   - Use the PR template (auto-loaded)
   - Specify content type and ownership area
   - Fill out the checklist

6. **Get reviewed**
   - Team areas: CODEOWNERS will review
   - User areas: self-reviewed
   - Feedback? Update and re-request review

7. **Merge & celebrate**
   - Squash/merge when ready
   - Your contribution is now available to the team!

## Directory Naming Conventions

- **Directories:** lowercase with hyphens (`my-skill-name`, NOT `my_skill_name` or `MySkillName`)
- **Files:** lowercase with extensions (`.py`, `.sh`, `.md`)
- **Special:** `.gitkeep` in empty directories to preserve structure

## Skill Metadata Format (SKILL.md)

```markdown
# My AI Skill

**Description:** One-sentence summary

**Author:** ncanen

**Team Area:** pde

**Capabilities:**
- Capability 1
- Capability 2

**Requirements:**
- Python 3.9+
- copilot-cli >= 1.0.50

**Usage:**
Brief usage example or link to README.md

**Status:** stable (or: experimental, deprecated)
```

## Review Checklist (for CODEOWNERS)

- [ ] Content is in the correct directory (team/ vs user/)
- [ ] README.md is present and clear
- [ ] No hardcoded secrets or credentials
- [ ] Naming conventions followed
- [ ] For skills: SKILL.md is properly formatted
- [ ] For services: Dockerfile and deployment docs included
- [ ] Code is well-commented where needed
- [ ] Dependencies are documented

## Questions?

- Check the README in the relevant content-type directory
- Review existing examples in the same area
- Open an issue for clarification

---

**Last updated:** Repository initialization
