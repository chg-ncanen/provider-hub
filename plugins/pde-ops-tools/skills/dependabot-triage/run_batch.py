#!/usr/bin/env python3
"""
Run the dependabot-triage skill against every current (non-archived) pde-*/chg-work*/chg-ws*/
chg-travel* repo in a GitHub org, in parallel -- cloning anything missing locally first.

Usage:
  run_batch.py [--libraries-only|--services-only] <base-path> [org] [max-parallel]

Example:
  run_batch.py ~/dev chghealthcare 6
  run_batch.py --libraries-only ~/dev chghealthcare 4   # only shared libraries, e.g. before a
                                                          # wider rollout of a downstream fix
  run_batch.py --services-only ~/dev chghealthcare 4    # everything else, e.g. after the
                                                          # libraries batch has already landed

For each repo in the authoritative GitHub list (via list_target_repos.py, archived repos
excluded) this:
  0. Clones it into <base-path>/<repo-name> if it isn't already there.
  1. Checks whether the local checkout is behind its remote or has uncommitted changes,
     and asks once whether to pull the stale ones before triaging.
  2. Runs `npm ci` (clean install matching the lockfile -- triage needs accurate `npm ls`).
  3. Runs the dependabot-triage skill headlessly, report-only -- it does NOT file Jira tickets.

Output: a timestamped report directory containing one <repo>.md (the triage report) and one
<repo>.log (full tool trace, for debugging a failed run) per repo.

NOTE: each triage run is launched with `claude -p ... --dangerously-skip-permissions`. That's
required because headless/parallel runs have no TTY to answer permission prompts -- which is
exactly why this script never dismisses alerts or files Jira tickets itself: dismissing an
alert and creating a ticket are the real external, hard-to-reverse side effects in this
workflow, and both deserve an actual approval (a permission prompt for ticket filing; an
explicit AskUserQuestion confirmation *and* a permission prompt for dismissals), not a rubber
stamp. Once this batch finishes, review the reports, then ask Claude (in a live, interactive
session) to dismiss confirmed non-actionable alerts and file the tickets -- see the skill's
step 7 for the dismissal flow and step 8 for the ticket format/defaults (one ticket per repo,
full report as the description, PDE-17837/PDE 1).

Requires: `claude`, `git`, `npm`, `gh` on PATH. No `jq`/`xargs`/`find` dependency, so this runs
unmodified on Windows, Linux, or macOS.
"""
import concurrent.futures
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from list_target_repos import list_target_repos  # noqa: E402

REQUIRED_COMMANDS = ("claude", "git", "npm", "gh")


def _require_commands():
    missing = [c for c in REQUIRED_COMMANDS if shutil.which(c) is None]
    if missing:
        for c in missing:
            print(f"Missing required command: {c}", file=sys.stderr)
        sys.exit(1)


def _clone_missing(targets, base_path):
    repos = []
    for name_with_owner in targets:
        repo_name = name_with_owner.rsplit("/", 1)[-1]
        local_dir = base_path / repo_name
        if not (local_dir / ".git").exists():
            print(f"Cloning missing repo: {name_with_owner} -> {local_dir}")
            proc = subprocess.run(
                ["gh", "repo", "clone", name_with_owner, str(local_dir)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            if proc.returncode == 0:
                repos.append(local_dir)
            else:
                print(f"  WARN: clone failed for {name_with_owner}, skipping", file=sys.stderr)
        else:
            repos.append(local_dir)
    return repos


def _current_branch(repo):
    proc = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True,
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _check_freshness(repos):
    stale = []
    for repo in repos:
        branch = _current_branch(repo)
        if not branch:
            continue
        subprocess.run(["git", "-C", str(repo), "fetch", "--quiet", "origin", branch],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        behind_proc = subprocess.run(
            ["git", "-C", str(repo), "rev-list", "--count", f"HEAD..origin/{branch}"],
            capture_output=True, text=True,
        )
        try:
            behind = int(behind_proc.stdout.strip())
        except ValueError:
            behind = 0
        dirty = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"],
            capture_output=True, text=True,
        ).stdout.strip()
        if behind > 0 or dirty:
            stale.append(repo)
    return stale


def _maybe_pull_stale(stale):
    print("These repos are behind their remote and/or have uncommitted changes:")
    for r in stale:
        print(f"  {r.name}")
    # `input()` raises EOFError immediately when stdin is closed (unattended/background
    # runs) -- default to "n" instead of crashing, same as the original bash's `|| ans="n"`.
    try:
        ans = input("Pull latest (fast-forward only) for these before triaging? [y/N] ")
    except EOFError:
        ans = "n"
    if ans.strip().lower() not in ("y", "yes"):
        print("Proceeding without pulling -- reports may reflect stale code.")
        return
    for repo in stale:
        dirty = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"],
            capture_output=True, text=True,
        ).stdout.strip()
        if dirty:
            print(f"  {repo.name}: skipping pull -- has uncommitted changes")
            continue
        proc = subprocess.run(["git", "-C", str(repo), "pull", "--ff-only"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"  {repo.name}: pulled" if proc.returncode == 0 else
              f"  {repo.name}: could not fast-forward, skipping")


def _run_one(repo, out_dir):
    name = repo.name
    log_path = out_dir / f"{name}.log"
    report_path = out_dir / f"{name}.md"
    try:
        with open(log_path, "w") as log:
            if not (repo / "package.json").exists():
                log.write(f"SKIP: no package.json in {name}\n")
                return

            log.write(f"=== {name}: npm ci ===\n")
            log.flush()
            proc = subprocess.run(["npm", "ci"], cwd=repo, stdout=log, stderr=log)
            if proc.returncode != 0:
                log.write(f"FAILED: npm ci for {name}\n")
                return

            log.write(f"=== {name}: dependabot-triage ===\n")
            log.flush()
            with open(report_path, "w") as report:
                subprocess.run(
                    ["claude", "-p",
                     "/dependabot-triage triage all open critical and high severity alerts "
                     "for this repo. Report only -- do not file any Jira tickets in this run.",
                     "--dangerously-skip-permissions"],
                    cwd=repo, stdout=report, stderr=log,
                )
            log.write(f"=== {name}: done, report at {report_path} ===\n")
    except Exception as e:
        # A single repo's unexpected failure (e.g. a transient tool error) shouldn't take
        # down the whole batch -- record it and let the summary step report it as FAILED.
        with open(log_path, "a") as log:
            log.write(f"FAILED: unexpected exception for {name}: {e}\n")


def main():
    libraries_only = False
    services_only = False
    positionals = []
    for arg in sys.argv[1:]:
        if arg == "--libraries-only":
            libraries_only = True
        elif arg == "--services-only":
            services_only = True
        else:
            positionals.append(arg)

    if libraries_only and services_only:
        print("--libraries-only and --services-only are mutually exclusive", file=sys.stderr)
        sys.exit(1)

    if not positionals:
        print("Usage: run_batch.py [--libraries-only|--services-only] "
              "<base-path-containing-repos> [org] [max-parallel]", file=sys.stderr)
        sys.exit(1)

    base_path = Path(positionals[0]).expanduser().resolve()
    org = positionals[1] if len(positionals) > 1 else "chghealthcare"
    max_parallel = int(positionals[2]) if len(positionals) > 2 else 4

    _require_commands()

    base_path.mkdir(parents=True, exist_ok=True)
    out_dir = base_path / f"dependabot-triage-reports-{datetime.now():%Y%m%d-%H%M%S}"
    out_dir.mkdir(parents=True)

    print(f"Fetching current repo list for org '{org}'...")
    try:
        targets = list_target_repos(org, libraries_only, services_only)
    except (ValueError, RuntimeError) as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

    # Optional: set REPO_FILTER to an extended-regex to scope the run to matching
    # nameWithOwner entries (e.g. REPO_FILTER='chg-work-schedule-service|pde-alerting-library'
    # for a small test).
    repo_filter = os.environ.get("REPO_FILTER")
    if repo_filter:
        pattern = re.compile(repo_filter)
        targets = [t for t in targets if pattern.search(t)]
        print(f"REPO_FILTER='{repo_filter}' applied -- {len(targets)} repo(s) remain")

    if not targets:
        print(f"No matching non-archived repos found for org {org}", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(targets)} current repo(s) in {org}:")
    for t in targets:
        print(f"  {t}")
    print()

    repos = _clone_missing(targets, base_path)
    print()

    if not repos:
        print("No repos available to triage after the clone step.", file=sys.stderr)
        sys.exit(1)

    print(f"Triaging {len(repos)} repo(s):")
    for r in repos:
        print(f"  {r.name}")
    print()

    stale = _check_freshness(repos)
    if stale:
        _maybe_pull_stale(stale)
        print()

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_parallel) as pool:
        futures = [pool.submit(_run_one, repo, out_dir) for repo in repos]
        for f in concurrent.futures.as_completed(futures):
            f.result()

    print()
    print("=== Summary ===")
    failed = []
    for repo in repos:
        name = repo.name
        report_path = out_dir / f"{name}.md"
        log_path = out_dir / f"{name}.log"
        log_text = log_path.read_text() if log_path.exists() else ""
        if report_path.exists() and report_path.stat().st_size > 0:
            print(f"  OK    {name}")
        elif any(line.startswith("SKIP:") for line in log_text.splitlines()):
            print(f"  SKIP  {name} (no package.json)")
        else:
            failed.append(name)
            reason = next((line for line in log_text.splitlines()
                           if line.startswith("FAILED:")), None)
            print(f"  FAIL  {name} -- {reason or f'see {log_path}'}")

    print()
    print(f"Done: {len(repos) - len(failed)}/{len(repos)} produced a report.")
    print(f"Reports: {out_dir}/*.md -- logs: {out_dir}/*.log")
    print("No Jira tickets were filed by this run (report-only, by design) -- ask Claude "
          "interactively to file tickets from these reports so each one gets a real "
          "approval prompt.")

    sys.exit(1 if len(failed) == len(repos) else 0)


if __name__ == "__main__":
    main()
