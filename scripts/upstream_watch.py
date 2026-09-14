#!/usr/bin/env python3
"""Weekly watch on the fork parent (tyvhb/SiftStack).

Reports only. Fetches, reads, prints. Never merges, commits, pushes, or edits
a tracked file.

The question this answers is not "does git report a conflict". On 2026-09-14 a
merge of the parked NJ branch came back with ZERO conflicts and would still
have deleted the whole SKU rehab engine, because the SKU commits were ancestral
to the merge base and git therefore saw nothing to resolve. A silent clean
merge is the failure mode, so every file both sides touched is reported as
REVIEW whether or not git predicts a conflict.

Exit codes: 0 nothing new, 1 new upstream commits to read, 2 the check failed
(never treat a failure as "nothing changed").
"""

from __future__ import annotations

import os
import subprocess
import sys

UPSTREAM_URL = "https://github.com/tyvhb/SiftStack"
UPSTREAM = "upstream"
# Overridable so the check itself can be tested against a known-changed state.
# A watcher that only ever prints "nothing new" is indistinguishable from one
# that is broken, so there has to be a way to make it fire on purpose.
OURS = os.environ.get("UPSTREAM_WATCH_OURS", "origin/main")
THEIRS = os.environ.get("UPSTREAM_WATCH_THEIRS", "upstream/main")

# Tokens that mark our New Jersey divergence. Counted in OUR version of a file
# so the report can say how much NJ-specific work sits in the thing upstream
# just rewrote. Deliberately broad: a false positive costs one line of reading,
# a false negative costs the work.
NJ_MARKERS = (
    "nj_", "NJ_", "njlispendens", "civilview", "surrogate",
    "Essex", "Middlesex", "Somerset", "Union", "Monmouth",
    "bands_for", "needs_manual_address", "lender_package", "nj-",
)


def git(*args: str, check: bool = True) -> str:
    r = subprocess.run(["git", *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout.strip()


def ensure_upstream() -> None:
    """Add the upstream remote if this clone lacks it. Never give it a pushurl."""
    remotes = git("remote").split()
    if UPSTREAM not in remotes:
        git("remote", "add", UPSTREAM, UPSTREAM_URL)
        print(f"  (added read-only remote {UPSTREAM} -> {UPSTREAM_URL})")
    # A parent repo is a fetch source, never a push target.
    git("remote", "set-url", "--push", UPSTREAM, "DISABLED", check=False)


def count_markers(rev: str, path: str) -> int:
    r = subprocess.run(["git", "show", f"{rev}:{path}"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return 0
    return sum(r.stdout.count(m) for m in NJ_MARKERS)


def main() -> int:
    ensure_upstream()
    git("fetch", UPSTREAM, "--quiet")
    git("fetch", "origin", "--quiet")

    base = git("merge-base", OURS, THEIRS)
    unmerged = [l for l in git("log", "--oneline", f"{OURS}..{THEIRS}").splitlines() if l]

    print("=" * 70)
    print(f"UPSTREAM WATCH  {UPSTREAM_URL}")
    print("=" * 70)

    if not unmerged:
        print("\nNothing new. Our fork already contains everything upstream has.")
        return 0

    print(f"\n{len(unmerged)} upstream commit(s) we have not merged:\n")
    for line in unmerged[:40]:
        print(f"  {line}")
    if len(unmerged) > 40:
        print(f"  ... and {len(unmerged) - 40} more")

    theirs_changed = set(git("diff", "--name-only", base, THEIRS).splitlines())
    ours_changed = set(git("diff", "--name-only", base, OURS).splitlines())
    both = sorted(theirs_changed & ours_changed)
    only_theirs = sorted(theirs_changed - ours_changed)

    # git's own prediction, kept only as a data point. It is NOT the verdict.
    mt = subprocess.run(
        ["git", "merge-tree", "--write-tree", "--name-only", OURS, THEIRS],
        capture_output=True, text=True)
    git_conflicts = {
        l for l in mt.stdout.splitlines()[1:]
        if l and not l.startswith(("Auto-merging", "CONFLICT"))
    } if mt.returncode == 1 else set()

    print(f"\n--- REVIEW: {len(both)} file(s) BOTH sides changed ---")
    if not both:
        print("  none")
    for f in both:
        nj = count_markers(OURS, f)
        pred = "git predicts a conflict" if f in git_conflicts else \
               "git predicts CLEAN -- silent-overwrite risk, read the diff"
        print(f"  {f}")
        print(f"      our NJ markers: {nj:<4} | {pred}")

    print(f"\n--- Upstream-only: {len(only_theirs)} file(s) we never touched ---")
    for f in only_theirs[:30]:
        print(f"  {f}")
    if len(only_theirs) > 30:
        print(f"  ... and {len(only_theirs) - 30} more")

    print("\n" + "-" * 70)
    print("Nothing was merged. To read one file's upstream diff:")
    print(f"  git diff {base[:9]}..{THEIRS} -- <path>")
    print("-" * 70)
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # a broken check must never read as "nothing new"
        print(f"UPSTREAM WATCH FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(2)
