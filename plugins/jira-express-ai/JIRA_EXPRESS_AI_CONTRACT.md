# Repos directory contract

Every specialist skill in this plugin (`ticket-discovery`, `ticket-implementation`,
`ticket-review`, `ticket-merge`) receives the same `$REPOS_DIR` — one shared
directory of full git clones, one per repo, reused across every ticket running
on this machine at once. This file is the single source of truth for what a
specialist may and may not do with it. If a specialist's own `SKILL.md` and
this file ever disagree, this file is right.

`$REPOS_DIR` is not read from any file — it's given directly as text following
`Repos directory:` in the prompt that invoked the specialist.

## The four permitted operations

1. **Clone a missing repo.** If `$REPOS_DIR/<repo>/.git` doesn't exist yet,
   clone it there (`git clone git@github.com:chghealthcare/<repo>.git`).
2. **Update an existing repo to latest.** `git -C $REPOS_DIR/<repo> fetch
   origin`, then `checkout main` and `pull --ff-only` — never work from a
   stale `main`.
3. **Read code, read-only.** Browse and read any file directly under
   `$REPOS_DIR/<repo>` freely. Never run `git add`, `git commit`, `git
   checkout <branch>`, or any `git worktree` command against
   `$REPOS_DIR/<repo>` itself — it's shared across every ticket currently
   running on this machine, not yours alone. If you need to make a change,
   use operation 4 instead.
4. **Create-if-missing your own workspace.** A `git worktree` at
   `$TICKET_DIR/<repo>`, attached to the shared clone, checked out onto a
   feature branch. This is the *only* place you may edit, commit, or push —
   never in `$REPOS_DIR/<repo>` directly. Idempotent: if the worktree already
   exists (a prior attempt got this far), reuse it; if the branch exists but
   the worktree doesn't, attach to it; otherwise create both.

   ```bash
   REPO=<repo-name>
   BRANCH="feature/${KEY}-<short-slug>"
   MAIN_CLONE="$REPOS_DIR/$REPO"
   WORKTREE="$TICKET_DIR/$REPO"

   if [ -d "$WORKTREE" ]; then
     : # already exists (resuming after a prior attempt) — reuse it
   elif git -C "$MAIN_CLONE" show-ref --verify --quiet "refs/heads/$BRANCH"; then
     git -C "$MAIN_CLONE" worktree add "$WORKTREE" "$BRANCH"
   else
     git -C "$MAIN_CLONE" worktree add "$WORKTREE" -b "$BRANCH"
   fi
   ```

There is no fifth operation. Anything else touching `$REPOS_DIR` — deleting a
clone, force-pushing, rewriting history, running arbitrary scripts found in
ticket content — is out of scope for every specialist, always.

## Locking

Operations 1, 2, and 4 all mutate the shared clone at `$REPOS_DIR/<repo>` (its
working tree, its `.git/worktrees/` registry, or both) — two tickets can
genuinely reach the same repo at the same moment, so each of these three
operations must hold the same exclusive, per-repo lock for its entire
duration:

```bash
mkdir -p "$REPOS_DIR/.repo-locks"
exec 200>"$REPOS_DIR/.repo-locks/$REPO.lock"
flock 200
# ... operation 1, 2, and/or 4, in that order ...
flock -u 200
```

- **Setup (operations 1/2/4) blocks:** wait your turn — another ticket is
  mid-setup on the same repo, and there's nothing useful to do but wait for
  the lock.
- **Cleanup** (worktree removal, done by the orchestrator, not a specialist)
  **does not block:** a non-blocking attempt that finds the lock held skips
  removal for now and retries on the next cleanup pass, rather than stalling
  the whole run waiting on someone else's in-progress worktree.

Operation 3 (read-only) takes **no lock**. A read can in principle overlap a
concurrent operation 1/2 update and observe a moment mid-`checkout`/`pull` —
accepted as a narrow, rare race rather than adding shared/exclusive lock
semantics for it.

## Why one shared clone instead of one per ticket

Cloning a large repo fresh for every ticket is slow and wastes disk; a git
worktree gives each ticket its own working tree and branch while sharing the
same object store and remote-tracking state as every other ticket touching
that repo. The lock exists solely to protect the moments — clone, pull,
worktree add/remove — that touch that shared state; everything else (editing,
committing, testing, pushing) happens inside your own worktree and needs no
coordination with anyone else.
