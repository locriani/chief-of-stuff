#!/usr/bin/env python3
"""One verdict per open pull or merge request, from its head pipeline and never the forge's own merge status (#419).

    chief-of-stuff merge-ready --root R [--repo owner/name]

The coordinator wrote "mergeable" from the forge's detailed merge status, which says only that there are no
conflicts, so a red pipeline and a request with open review findings were both called mergeable. This prints
one line per open request: the verdict (ready | red | running | conflicts | findings open | stale pipeline |
no pipeline), the head sha, the head pipeline's id, status and the sha it ran on, conflicts, and unresolved
review threads. "Mergeable" is written only from a `ready` line, quoting its pipeline id and sha. It never
merges and needs no approver or merge_owner setting; merge_approved.py lists the same lines for `worker`.
GitHub reads its pull requests with `gh pr list` and one `gh api graphql` call (review threads, the head
commit and its workflow run id); GitLab reads merge requests and their discussions over REST.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path
from urllib.parse import urlencode

import backlog
from forge_review import Request, forge, github_requests, gitlab_requests
from settings import SettingsError
from workspace import ConfigError

# ponytail: first 100 open requests, threads and checks each; paginate when a request outgrows that.
QUERY = """query($owner: String!, $name: String!) { repository(owner: $owner, name: $name) {
  pullRequests(states: OPEN, first: 100) { nodes { number
    reviewThreads(first: 100) { nodes { isResolved } }
    commits(last: 1) { nodes { commit { oid statusCheckRollup { contexts(first: 100) { nodes {
      __typename ... on CheckRun { checkSuite { workflowRun { databaseId } } } } } } } } } } } } }"""


def github_ready(repo: str, gh=backlog.run_gh) -> list[Request]:
    owner, name = repo.split("/", 1)
    code, out, err = gh(["api", "graphql", "-f", f"query={QUERY}", "-F", f"owner={owner}", "-F", f"name={name}"])
    if code != 0:
        raise RuntimeError(f"gh api graphql: {err.strip() or code}")
    nodes = {n["number"]: n for n in json.loads(out)["data"]["repository"]["pullRequests"]["nodes"]}
    found = []
    for r in github_requests(repo, "", gh):
        n = nodes.get(r.number)
        if n is None:
            raise RuntimeError(f"#{r.number}: not in the graphql read")
        commit = n["commits"]["nodes"][0]["commit"]
        rollup = commit.get("statusCheckRollup")
        ids = [str(run["databaseId"]) for c in (rollup or {}).get("contexts", {}).get("nodes", [])
               if (run := ((c.get("checkSuite") or {}).get("workflowRun") or {}))]
        found.append(dataclasses.replace(
            r, pipeline_id=ids[0] if ids else "", pipeline_sha=commit["oid"] if rollup else "",
            threads=sum(not t["isResolved"] for t in n["reviewThreads"]["nodes"])))
    return found


def gitlab_ready(home: backlog.Backlog, project: str, token: str, call=backlog._call) -> list[Request]:
    base = home.api(project)
    found = []
    for r in gitlab_requests(home, project, "", token, call):
        discussions, _, err = call("GET", f"{base}/merge_requests/{r.number}/discussions?" + urlencode({"per_page": 100}),
                                   token, backlog.TIMEOUT)
        if err:
            raise RuntimeError(f"!{r.number}: {err}")
        open_threads = sum(any(n.get("resolvable") and not n.get("resolved") and not n.get("system")
                               for n in d.get("notes") or []) for d in discussions)
        found.append(dataclasses.replace(r, threads=open_threads))
    return found


def read(home, github: bool, repo: str, gh=backlog.run_gh, call=backlog._call) -> list[Request]:
    return github_ready(repo, gh) if github else gitlab_ready(home, repo, backlog.token(home), call)


def line(r: Request, mark: str) -> str:
    pipeline = "none" if r.pipeline == "none" else f"{r.pipeline_id or '-'} {r.pipeline} on {r.pipeline_sha[:7]}"
    return (f"{mark}{r.number} {r.verdict} · head {r.head[:7]} · pipeline {pipeline} · "
            f"conflicts {'yes' if r.mergeable == 'conflict' else 'no'} · unresolved threads {r.threads} · {r.url}")


def main(argv: list[str] | None = None, gh=backlog.run_gh, call=backlog._call) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, required=True, help="workspace holding CLAUDE.md")
    ap.add_argument("--repo", help="owner/name or group/name; default the backlog's")
    args = ap.parse_args(argv)
    try:
        _, home, github, repo = forge(args.root, args.repo)
    except (OSError, ConfigError, SettingsError) as exc:
        print(f"merge-ready: {exc}", file=sys.stderr)
        return 2
    try:
        found = read(home, github, repo, gh, call)
    except (RuntimeError, ValueError, KeyError, TypeError, AttributeError) as exc:
        print(f"merge-ready: {exc}", file=sys.stderr)
        return 1
    for r in sorted(found, key=lambda r: r.number):
        print(line(r, "#" if github else "!"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
