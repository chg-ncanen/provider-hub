# Dependency-bump playbook

Scope: npm repos (`package.json` + `package-lock.json`). A repo using
yarn/pnpm needs an equivalent command for each rule below — the principles
still apply, the literal commands don't.

## Discovery guidance

- **The range in `package.json` is not yours to redraw.** It already
  encodes a real decision — how much automatic movement (patch/minor within
  the same major) the team considers safe without review. Your job is to
  move the *resolved* version forward inside that boundary, never to widen
  or change the boundary itself.
- **Check whether the patched version actually fits the existing range
  before recommending anything.** Use `npm view <pkg>@<existing-range>
  version` (or read the lockfile) to find the real latest-in-range version
  right now — not a version number copied from the ticket, which may be
  stale by the time this runs.
  - **Fits the range:** recommend `npm update <pkg>` (lockfile-only, no
    manifest edit) — never `npm install <pkg>@<version>`, which
    unconditionally rewrites the manifest's declared specifier even when
    the old range already covered the target.
  - **Doesn't fit the range:** whether that's a blocker depends on two
    separate questions — reachability and bump size — not on dependency
    type alone. The underlying rule is "don't cross a version boundary you
    can't verify is safe"; respecting the range is only one way to honor
    that. When you can verify directly, the range stops being the only
    signal.
    1. **Is the vulnerable/changed code actually reachable in this
       service?** Use the same reachability analysis you'd apply to any
       alert (disabled exporters, unused routes, unused config, dead code
       paths, etc.). If it's confirmed unreachable, there's no live exploit
       to fix urgently — this holds regardless of whether the package is a
       production dependency or a devDependency. Say so in Risks, recommend
       tracking the range-widening as separate planned maintenance rather
       than urgent security work, and don't force an automatic bump just
       because it's technically possible. Don't silently drop it from the
       ticket, either.
    2. **If it is reachable, does the fix cross a real major-version
       boundary?** A "real" major means the package's own versioning
       signals an intentional breaking change (e.g. `4.x` → `5.x`, `1.x` →
       `2.x`), or its changelog/release notes say so explicitly. A 0.x
       package moving between minor lines (`0.62.x` → `0.65.x`) is
       **not** a real major for this purpose — it's out of range only
       because npm's caret semantics pin `0.x` to the same minor, not
       because the author signaled a breaking change.
       - **Real major, reachable, production/runtime dependency** (listed
         under `dependencies` in `package.json`, or otherwise shipped):
         this is genuine breaking-change risk that nothing in this
         pipeline can verify (tests don't see production traffic, other
         services' assumptions, or business-logic edge cases the package
         might change behavior on). Say so explicitly in `discovery.md`'s
         Risks section and let a human decide whether to take it. Do not
         widen the range yourself and do not silently drop the package
         from Tier 1.
       - **Not a real major (0.x same-major-line move, or a
         changelog-confirmed non-breaking release), reachable,
         production/runtime dependency:** treat like the devDependency case
         below — recommend widening the range and taking the bump,
         *provided implementation actually verifies it* (see Implementation
         guidance). Name it as a range-widening exception in the Proposed
         Approach and state what verification implementation needs to run
         to trust it.
       - **devDependency** (test runner, linter, build tool — never
         shipped, listed under `devDependencies`): a real major bump is
         fine to recommend without escalating to a human, *provided
         implementation actually verifies it* (see Implementation guidance
         below) rather than trusting semver. Say so in the Proposed
         Approach as a major bump — name it as one — and note what
         verification implementation needs to run to trust it (usually:
         the full test/lint/build suite, plus a look for any
         config/CLI-flag changes the new major version might have made).
- **A direct-dependency bump naturally cascades into transitive packages
  the ticket never named** (nested/duplicate copies, sibling packages
  moving version, new indirect deps) — this is expected and not itself a
  concern, even when a transitive package's version moves outside where it
  previously resolved. Document the cascade in the Proposed Approach, and
  note that the exact cascade is a function of registry state at install
  time — implementation must re-diff for itself rather than trust
  discovery's numbers, since new releases may land upstream before
  implementation runs. The only thing that needs the same escalation as an
  out-of-range direct bump (above) is if fixing this ticket requires
  *forcing* any package — direct or transitive — to a version outside what
  normal resolution would produce (an `overrides`/`resolutions` entry, a
  manual pin) rather than letting the dependency graph settle there
  naturally. That's a deliberate override of someone else's declared
  compatibility contract, not a side effect of updating — treat it with the
  same production-vs-devDependency care as an out-of-range direct bump.
- Set `**Playbook:** dependency-bump` in `discovery.md`'s header (next to
  `**Status:**`) when this playbook applied, so implementation knows to
  load this same file's Implementation guidance without re-deriving the
  category itself.

## Implementation guidance

- Run `npm update <pkg> [<pkg> ...]` for every package discovery confirmed
  is in-range. Confirm afterward that `package.json` is byte-identical to
  before — if it changed, something ran an `install` instead of an
  `update` and needs to be redone.
- Diff the full lockfile against its pre-change state — not just the
  packages discovery named. Enumerate every package whose resolved version
  actually changed (including newly-added nested copies) in
  `implementation-notes.md`'s Changes Made table and in the PR body. Do not
  understate the diff to match the ticket's named package list.
- Don't just trust that "a version changed" means the vulnerability is
  gone. After the update, re-run the alert scan this ticket is based on
  (`npm audit`, `gh api .../dependabot/alerts`, etc.) and confirm every
  alert the ticket named is actually gone. If one isn't, that's a genuine
  blocker — use the Blocked path rather than opening a PR that doesn't fix
  what it claims to.
- If discovery flagged a **production/runtime** package as needing a *real
  major-version* bump (a genuine breaking-change signal — not just a 0.x
  package's same-major-line move), do not implement that part yourself — it
  needs a human decision on whether to take the breaking change. Implement
  only the in-range packages and carry the flagged one forward as a note
  for the human, the same way discovery flagged it.
- If discovery flagged a **production/runtime** package as needing a
  *range-widening, non-major* bump (a 0.x package's same-major-line move,
  or a changelog-confirmed non-breaking release), or a **devDependency** as
  needing any out-of-range bump, you may implement it yourself — but a
  clean install with no further check is not verification. Run the full
  test/lint/build suite and confirm it's actually green. If the bump
  changed any CLI flag, config default, or script/runtime behavior (new
  releases often do), find every one of those differences and explicitly
  handle each — fix the script, add config to restore the old behavior,
  whatever's needed — rather than letting an unexamined difference ride
  into the PR. State plainly in `implementation-notes.md` and the PR body
  that this is a range-widening bump, plus a one-line reason it's safe
  (e.g. "full suite green; only behavior change was X, handled by Y"). If
  you can't get a clean, understood verification, that's a genuine
  blocker — don't ship an unverified out-of-range bump just because it
  isn't a "real" major.
