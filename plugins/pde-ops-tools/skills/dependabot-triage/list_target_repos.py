#!/usr/bin/env python3
"""
List current (non-archived) GitHub repos in an org that are in scope for dependabot-triage
batches. Candidates are name-matched (pde-*/chg-work*/chg-ws*/chg-travel*, case-insensitive)
to keep the API-call count reasonable, then each candidate's ownership is verified against
its CODEOWNERS file -- that's the actual source of truth, since naming doesn't line up
cleanly (e.g. chg-workunit-service and chg-travel-trans* match the name prefixes but have
no CODEOWNERS entry for this team at all, confirmed via the API).

Usage:
  list_target_repos.py [--libraries-only|--services-only] [org] [output-file] [codeowners-team]

Examples:
  list_target_repos.py                          # org=chghealthcare, team=@chghealthcare/pde, prints to stdout
  list_target_repos.py chghealthcare repos.txt   # also writes the list to repos.txt
  list_target_repos.py --libraries-only          # only repos whose name matches *library*/*-utils
  list_target_repos.py --services-only           # everything else (services/apps/UIs, not libraries)

--libraries-only restricts the output to shared libraries (name contains "library" or ends in
"-utils") -- most service/app repos depend on these, so they're the ones that typically need
updating first before a dependabot fix can land downstream. --services-only is the complement
of that same name pattern, for running a batch after the libraries have already been handled.

Output: one "owner/repo" per line, sorted -- archived repos excluded, ownership verified.

Requires: `gh` on PATH and authenticated. No `jq` dependency -- JSON is parsed in Python, so
this runs unmodified on Windows, Linux, or macOS.
"""
import base64
import json
import re
import shutil
import subprocess
import sys

NAME_PATTERN = re.compile(r"^(pde-|chg-work|chg-ws|chg-travel)", re.IGNORECASE)
LIBRARY_PATTERN = re.compile(r"library|-utils$", re.IGNORECASE)
CODEOWNERS_PATHS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")


def _require(cmd):
    if shutil.which(cmd) is None:
        print(f"Missing required command: {cmd}", file=sys.stderr)
        sys.exit(1)


def _candidates(org):
    result = subprocess.run(
        ["gh", "repo", "list", org, "--limit", "2000",
         "--json", "name,nameWithOwner,isArchived"],
        capture_output=True, text=True, check=True,
    )
    repos = json.loads(result.stdout)
    names = [r["nameWithOwner"] for r in repos
             if not r["isArchived"] and NAME_PATTERN.search(r["name"])]
    return sorted(names)


def _is_owned(name_with_owner, team):
    # Only the first CODEOWNERS path that actually has content decides ownership --
    # matches the original bash behavior of not falling through to the next path
    # once content is found, even if that content doesn't mention the team.
    for path in CODEOWNERS_PATHS:
        proc = subprocess.run(
            ["gh", "api", f"repos/{name_with_owner}/contents/{path}", "--jq", ".content"],
            capture_output=True, text=True,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            continue
        try:
            content = base64.b64decode(proc.stdout.strip()).decode("utf-8", errors="ignore")
        except Exception:
            continue
        if not content:
            continue
        return team.lower() in content.lower()
    return False


def list_target_repos(org="chghealthcare", libraries_only=False, services_only=False,
                       team="@chghealthcare/pde"):
    if libraries_only and services_only:
        raise ValueError("--libraries-only and --services-only are mutually exclusive")

    _require("gh")

    candidates = _candidates(org)
    if not candidates:
        raise RuntimeError(
            f"No candidate repos found in {org} matching pde-*/chg-work*/chg-ws*/chg-travel*"
        )

    owned = [nwo for nwo in candidates if _is_owned(nwo, team)]
    if not owned:
        raise RuntimeError(f"No candidates in {org} have a CODEOWNERS entry for {team}")

    if libraries_only or services_only:
        def is_library(nwo):
            return bool(LIBRARY_PATTERN.search(nwo.rsplit("/", 1)[-1]))
        owned = [nwo for nwo in owned if is_library(nwo) == libraries_only]
        if not owned:
            raise RuntimeError("No owned repos matched the requested filter")

    return sorted(owned)


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

    org = positionals[0] if len(positionals) > 0 else "chghealthcare"
    out_file = positionals[1] if len(positionals) > 1 else None
    team = positionals[2] if len(positionals) > 2 else "@chghealthcare/pde"

    try:
        repos = list_target_repos(org, libraries_only, services_only, team)
    except (ValueError, RuntimeError) as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

    output = "\n".join(repos)
    print(output)

    if out_file:
        with open(out_file, "w") as f:
            f.write(output + "\n")
        print(f"Wrote {len(repos)} repos to {out_file}", file=sys.stderr)


if __name__ == "__main__":
    main()
