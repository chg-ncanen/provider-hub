---
name: setup-companion-tools
description: Interactively install optional companion MCPs/plugins for PDE work (Grafana, LogRocket, Atlassian, Salesforce prod) that aren't bundled in the pde plugin. Use when the user asks to set up, connect, install, or configure additional PDE tools/MCPs, or asks what companion tools are available.
---

# Setup Companion Tools

Helps a developer optionally install MCP servers/plugins commonly used alongside PDE tooling, that
aren't bundled in the `pde` plugin itself: Grafana (`gcx`), LogRocket, Atlassian (Jira/Confluence),
and Salesforce prod (needed by `resolve-duplicate-contact-alerts`). Nothing here runs automatically —
only when a developer explicitly invokes this skill, and only for whichever service(s) they pick.

## Before you start

Figure out which CLI you're actually running under (a machine can have both installed) — check for
`CLAUDECODE`/`CLAUDE_CODE_SESSION_ID` in the environment for Claude Code, or otherwise confirm with
the user directly if genuinely ambiguous. Pass `--cli claude` or `--cli copilot` to every
`manage_companions.py` call below — the two CLIs use different commands and config locations.

## Flow

1. Run `python3 manage_companions.py status --cli <claude|copilot>` (from this skill's own
   directory) to see what's already installed.
2. Present the four services to the user with their current status — use a multiple-choice
   prompt. Let them pick **one at a time**; after handling their pick, ask if they want another, and
   repeat until they say they're done. Don't install anything they didn't explicitly choose.
3. If `status` already shows their pick installed, say so and go back to step 2 — nothing to do.
4. Otherwise, run `python3 manage_companions.py install <service> --cli <claude|copilot>` and
   relay the result (`success`, what got installed, or the `error` if it failed).
5. **Special case — `salesforce-prod`**: registering the MCP entry doesn't need the `sf` CLI, but
   actually *using* it (via `resolve-duplicate-contact-alerts`) does, and `install` doesn't handle
   that part. Check `_sf_cli` in the `status` output:
   - `installed: false` → run `python3 manage_companions.py sf-cli-guidance`, which detects the
     OS (Linux/macOS/Windows) and returns the right sudo-free vs. sudo-needed install command for
     that system. Relay it — **don't attempt to install `sf` yourself**; it may need root, and you
     have no way to supply a password interactively even if it does.
   - `installed: true` but `prod_alias: false` → tell them to run `sf org login web --alias prod`
     (an interactive browser login — something only they can do).

## Available services

- **Grafana (`gcx`)** — 16+ skills, a `grafana-debugger` agent, dashboard/alert/SLO management.
  Same install mechanism on both CLIs.
- **LogRocket** — session replay, metrics, issue search. Same install mechanism on both CLIs.
- **Atlassian** — Jira/Confluence search, issue creation, sprint management.
  - Claude Code: the full official plugin (6 skills), via the pre-registered
    `claude-plugins-official` marketplace.
  - Copilot CLI: that marketplace file fails to parse there (a real schema incompatibility on the
    `source` field of several entries, not a typo) — `install` falls back to registering the bare
    `chg-atlassian` MCP endpoint instead. Tools only, no bundled skills, until that gets fixed
    upstream.
- **Salesforce prod** — SOQL queries against the prod org. Also needs the `sf` CLI authenticated to
  the `prod` alias (see step 5 above) — the skill that actually uses this is
  `resolve-duplicate-contact-alerts`.
