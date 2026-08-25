# Live-site-bug (backend) playbook

Scope: tickets whose **fix** lives in a backend service repo PDE owns (e.g.
`pde-travel-service`, `pde-providers-service-next`/PSN,
`pde-experience-service`/PES, `pde-auth-service`/PAS,
`pde-assignment-details-service`, `pde-availability-service`). This
playbook never produces a React/Vue/native-app frontend-framework code
change — in `pde-ui`, another UI repo, or the native app — even when the
ticket is filed under a frontend prefix, reports a frontend symptom, or its
own fix guidance asks for a frontend change alongside a backend one. The
exclusion is about the kind of code, not a specific repo name: PDE has more
than one frontend repo, and all of them are out of scope here. A separate
playbook, not yet written, will cover tickets whose fix is genuinely
frontend-framework code.

## Discovery guidance

- **Triage the ticket into one of four shapes before researching further**
  — this determines what "Proposed Approach" is even allowed to claim:
  - *Backend, root-caused* — the ticket already names a file/line and a
    fix, in a backend repo.
  - *Backend, investigation-only* — no hypothesis yet, but the symptom
    points at a backend service; try to form one.
  - *Mixed* — the ticket (however it's filed, including under a frontend
    prefix) describes or implies a frontend mitigation *and* a backend
    root cause — see the bullet below; only the backend slice is ever in
    scope.
  - *Frontend-framework* — the only fixable/confirmable cause is
    React/Vue/native-app framework code, with no backend slice at all.
    Out of scope for this playbook entirely.
- **Treat every claim — the ticket, its comments, and the ASA
  common-alerts Confluence runbook — as a hypothesis, never a fact.**
  Write it into `discovery.md`'s `Current Behavior`/`Proposed Approach`
  explicitly labeled (e.g. "Hypothesis (unconfirmed): ..."), regardless of
  how confident or detailed it reads, or whether it was already validated
  once before. This includes the Confluence runbook: a matching symptom is
  another input to the hypothesis, not corroboration that makes it true —
  the runbook itself can be stale. `ticket-discovery` cannot verify
  anything by running code (it stays read-only) — verification is
  `ticket-implementation`'s job.
- **Investigate freely across repo boundaries, including into frontend
  repos, to trace a symptom back to its real cause** (e.g. confirming a
  gateway/experience-service layer proxies a request to the backend
  service actually producing the error). Reading a repo to understand a
  symptom is never the same thing as fixing it there.
- **For a Mixed ticket, pursue the backend slice only — never touch
  frontend-framework code to "also" fix it.**
  - If a backend root cause is found and confirmed (per implementation's
    reproduce-before-fix discipline below), recommend fixing **only
    that**, in the backend repo, as its own single-repo PR.
  - Explicitly note, in `discovery.md`, that the ticket also calls for a
    frontend mitigation out of scope for this pipeline and needing
    separate frontend work — don't silently drop it, the same way an
    out-of-range major dependency bump gets carried forward as a named
    note rather than dropped.
  - If the *only* real, confirmable fix is frontend-framework code,
    that's a `BLOCKED` outcome — not `NO_CHANGES_NEEDED` (a change
    genuinely is needed, just not one this pipeline can make). Say
    plainly in `Suggested Next Step` that this needs a frontend-framework
    fix.
- **Use Grafana/LogRocket tools if available in this session** to
  sanity-check time-sensitive claims (is this still happening? current
  frequency? already mitigated?) with the same skepticism as any other
  input. If they aren't available, say so plainly in `discovery.md` and
  continue — never block or degrade the rest of discovery over this.
- **Don't copy real PII into any artifact.** Some tickets link directly to
  real production records (a Salesforce provider record, a specific Okta
  account). Refer to "the affected provider" and link back to the Jira
  ticket instead of copying names, Okta IDs, emails, or record IDs into
  `discovery.md`, the PR body, or the Confluence mirror.
- **If the actual fix (not the investigation) would need a PR in more
  than one repo, that's a `BLOCKED` outcome** — the pipeline only handles
  one PR per ticket. Tell the developer to split the ticket into one per
  repo, the same way a multi-repo dependency-bump ticket already gets
  split into per-repo clones. Investigating across repos to find this out
  is expected and not itself a reason to block.
- Set `**Playbook:** live-site-bug-backend` in `discovery.md`'s header
  (next to `**Status:**`) when this playbook applied, so implementation
  knows to load this same file's Implementation guidance without
  re-deriving the category itself.

## Implementation guidance

- **Reproduce before you fix.** Before writing any fix code, write a test
  that should fail, for the hypothesized reason, against current code —
  mocking the failing dependency where the cause is external (e.g. force
  a wrapped client call to reject repeatedly to exercise a give-up
  branch, rather than needing a real outage). Apply the
  `superpowers:test-driven-development` red/green cycle rather than
  restating it here.
  - *Reproduces as hypothesized:* proceed with the normal TDD red→green
    cycle. The passing test doubles as regression coverage. State in
    `implementation-notes.md` that the hypothesis was empirically
    confirmed, not just plausible.
  - *A test genuinely isn't feasible* (a pure infra/config value with no
    code representation in the owned repo): say so explicitly in
    `implementation-notes.md`, and only proceed if there's other clear,
    well-supported certainty — state exactly what that evidence is (e.g.
    a Grafana-confirmed incident timeline plus an unambiguous one-line
    config change). Otherwise, `BLOCKED`.
  - *A test is feasible but doesn't reproduce the hypothesized failure:*
    take one further investigation pass — implementation has tools
    discovery didn't (it can run code). If that finds and confirms a
    different real cause, proceed with it and document the correction
    explicitly — a live-site ticket's stated cause turning out wrong on
    closer inspection is a normal outcome for this category, not a
    failure to smooth over. If it still can't reach a reproducing test or
    other clear certainty, `BLOCKED` — state what was tried and what a
    developer needs to do next (e.g. pull more LogRocket sessions, needs
    infra access this pipeline doesn't have).
  - The bias is: no reproduction and no other stated certainty means
    stop, not guess. This is a stricter bar than `dependency-bump.md`'s
    "verify a major bump with the full test suite" — there, the ticket's
    core claim (a CVE exists) is independently mechanically true; here,
    the ticket's core claim (this is why it's broken) is exactly what's
    in question.
- **Keep the fix surgical.** Once a cause is confirmed, change only what
  the fix actually requires — don't refactor surrounding code, rename
  things, reorganize files, or fix unrelated issues you notice along the
  way, even if they look like quick wins. A live-site fix is judged on how
  small and reviewable its diff is, not how much of the neighborhood it
  improves. Note anything else worth fixing as a follow-up in
  `implementation-notes.md` rather than folding it into this PR.
- **If mid-implementation it becomes clear the fix needs a second repo**
  (e.g. a shared contract change), stop and go `BLOCKED` with the same
  split-into-per-repo-tickets guidance — do not open a second PR. This
  overrides `ticket-implementation/SKILL.md` Step 5's default multi-repo
  handling ("write `review-context.md` for the primary repo, list the
  others in Notes"), which is fine for an incidental cross-repo edit on
  an ordinary ticket but wrong here, where crossing repos is common and
  expected.
