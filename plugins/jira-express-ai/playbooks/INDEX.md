# Ticket-work playbooks

A playbook is standing guidance for a recurring category of ticket work —
things worth getting right every time without a human having to repeat
themselves in a comment. Both `ticket-discovery` and `ticket-implementation`
check this index; each playbook file covers both agents in its own section
(`## Discovery guidance` / `## Implementation guidance`) so a category's
knowledge doesn't drift into two documents.

Match a ticket to a category by its summary/description content, not its
Jira labels or project — none of these categories have a dedicated label.
If a ticket clearly matches more than one, read all matching playbooks. If
none match, proceed with no playbook — this index only ever adds guidance,
it never gates or blocks normal ticket flow.

| Category | Trigger | File |
|---|---|---|
| dependency-bump | The ticket's ask is a Dependabot/CVE triage or a routine dependency version bump — moving an existing dependency to a patched or newer version, not a behavioral code change. | `dependency-bump.md` |
