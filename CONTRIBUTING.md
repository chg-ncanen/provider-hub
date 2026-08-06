# Contributing to provider-hub

Thank you for contributing! This guide explains where things go and how the review process works.

## Content Types & Where They Live

### Plugins
**Directory:** `plugins/<plugin-name>/`

A plugin bundles everything needed to install and use a skill (or set of skills) from *any*
project — skill(s), the MCP server(s) they need, hooks, and setup scripts. Both Claude Code and
GitHub Copilot CLI install from the same `.claude-plugin/` layout, no forking needed.

**Include:**
- `.claude-plugin/plugin.json` — plugin metadata (`name` is the actual install/namespace identity,
  used for `claude plugin install <name>@provider-hub` and skill invocation as
  `/<name>:<skill-name>` — it does not need to match the folder name, but keep them in sync to avoid
  confusion)
- `skills/<skill-name>/` — each with a `SKILL.md` (see [Skill Metadata Format](#skill-metadata-format-skillmd))
- `.mcp.json` / `mcp-servers/` — any MCP server(s) the skills depend on
- `README.md` — what it provides, required credentials, how to install and use it

**Example:**
```
plugins/pde-ops-tools/
  ├── .claude-plugin/plugin.json
  ├── .mcp.json
  ├── mcp-servers/pde-mcp/
  ├── skills/
  │   ├── resolve-duplicate-contact-alerts/
  │   └── dependabot-triage/
  └── README.md
```

- A plugin can't reference files outside its own directory once installed — anything it needs must
  either live inside `plugins/<plugin-name>/` or be a real package dependency (see Shared below).

### Shared
**Directory:** `shared/<name>/`

Library code that a plugin depends on but that isn't itself installable — e.g. a Python package
pulled in via `requirements.txt` rather than bundled as a copy. Not reachable from outside this
repo; only useful as a dependency of something under `plugins/`.

**Include:**
- Source files
- Dependency/package metadata (`pyproject.toml`, etc.)
- `README.md`

**Example:** `shared/pde-ops-api` — depended on by `plugins/pde-ops-tools/mcp-servers/pde-mcp` via
`requirements.txt` (`pde-ops-api @ git+https://github.com/chg-ncanen/provider-hub.git#subdirectory=shared/pde-ops-api`),
not copied in.

If something doesn't need standalone distribution and isn't depended on by a plugin, it likely
doesn't belong in this repo at all — keep experiments and one-off scripts in their own project until
they're ready to become a real plugin or shared dependency.

## Ownership Model

`plugins/` and `shared/` are team-owned — changes require review from CODEOWNERS (see
`.github/CODEOWNERS`). As new areas/teams contribute, add a line per top-level plugin or shared
package rather than reintroducing a team/user directory split.

## How to Contribute

1. **Create a branch**
   ```bash
   git checkout -b feat/my-skill-name
   ```

2. **Add your content** under `plugins/<plugin-name>/` or `shared/<name>/`

3. **Include documentation**
   - README.md explaining what it is and how to use it
   - For skills: SKILL.md with metadata

4. **No secrets!**
   - Never commit credentials, API keys, or sensitive data
   - Use environment variables or config files (in .gitignore)

5. **Submit a PR**
   - Use the PR template (auto-loaded)
   - Fill out the checklist

6. **Get reviewed**
   - CODEOWNERS will review
   - Feedback? Update and re-request review

7. **Merge & celebrate**
   - Squash/merge when ready
   - Your contribution is now available to the team!

## Directory Naming Conventions

- **Directories:** lowercase with hyphens (`my-skill-name`, NOT `my_skill_name` or `MySkillName`)
- **Files:** lowercase with extensions (`.py`, `.sh`, `.md`)
- **Plugins & skills:** prefix with team/area (e.g., `pde-ops-tools`, `pde-ai-ticket-discovery`)

## Skill Metadata Format (SKILL.md)

The YAML frontmatter is not optional decoration — it's what Claude Code and Copilot CLI parse to
discover the skill and decide when to invoke it. `name` must match the skill's directory name.
`description` must state the trigger condition ("Use when...") — that's the only text the agent
sees when deciding whether this skill is relevant, so vague descriptions ("helps with tickets")
mean the skill never gets picked up. Everything below the frontmatter is free-form and for human
reviewers.

```markdown
---
name: pde-ai-ticket-discovery
description: Use when triaging incoming support tickets to auto-classify and route them to the right queue
---

# PDE AI Ticket Discovery

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

- [ ] Content is in the correct directory (`plugins/` vs `shared/`)
- [ ] README.md is present and clear
- [ ] No hardcoded secrets or credentials
- [ ] Naming conventions followed
- [ ] For skills: SKILL.md has valid YAML frontmatter (`name` matches directory, `description` states the trigger condition)
- [ ] Dependencies are documented

## Questions?

- Check the README in the relevant `plugins/<name>/` or `shared/<name>/` directory
- Review existing examples in the same area
- Open an issue for clarification
