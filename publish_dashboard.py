#!/usr/bin/env python3
"""Publish regenerated dashboard snapshots to the default branch WITHOUT rebasing.

Why this file exists (root cause of the copybot-tick publication failures)
-------------------------------------------------------------------------
`copybot.yml` used to publish with:

    git commit -m "copybot ..."
    git pull --rebase --autostash origin main
    git push origin HEAD:main

A *queued* GitHub Actions run is checked out at the commit that existed when the
run was **created**, not when the job finally starts. copybot jobs run for up to
~an hour and are serialised by one concurrency group, so runs queue. By the time a
queued run starts, `origin/main` has usually moved several commits ahead -- and
every one of those commits is a full rewrite of `docs/data.json` and
`docs/quality.json`. Rebasing one full rewrite on top of another full rewrite
therefore produces, every single time:

    CONFLICT (content): Merge conflict in docs/data.json
    CONFLICT (content): Merge conflict in docs/quality.json
    error: could not apply <sha>

Retrying that identical rebase cannot help: the local commit is still based on the
same stale commit and the files still conflict in the same way, so all 3 attempts
fail and the run ends with `publication_failed=1`.

The dashboard files are *derived snapshots* of `copybot.db` (which lives in the
GitHub Actions cache), not hand-edited data, so the correct conflict resolution is
"the newest snapshot wins". This publisher therefore never rebases, never merges,
never force-pushes and never drops commits:

  1. `git fetch origin main` and take the current remote tip.
  2. If the remote already holds our exact bytes -> nothing to publish.
  3. If the remote holds a *newer* snapshot (higher `"updated"` timestamp) ->
     leave it alone, so a stale run can never roll the dashboard backwards.
  4. Otherwise rebuild our commit directly on top of the remote tip
     (`git reset --hard <tip>` + re-write the snapshot bytes) and push.
     The push is a plain fast-forward; if another writer won the race in the
     meantime the push is simply retried against the new tip.

Trading history is untouched: nothing here reads or writes the SQLite DB, and the
repo history keeps every earlier commit.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_REMOTE = "origin"
DEFAULT_BRANCH = "main"
DEFAULT_ATTEMPTS = 5
DEFAULT_RETRY_SLEEP = 5.0


class PublishError(RuntimeError):
    """Raised for unrecoverable publishing problems (bad file, dirty tree, ...)."""


class Git:
    """Thin wrapper around the `git` CLI so behaviour is easy to unit test."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def run(self, *args: str, check: bool = True, text: bool = True):
        proc = subprocess.run(
            ["git", *args],
            cwd=str(self.root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=text,
        )
        if check and proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "")
            if isinstance(detail, bytes):
                detail = detail.decode("utf-8", "replace")
            raise PublishError(f"git {' '.join(args)} failed: {detail.strip()}")
        return proc

    def out(self, *args: str) -> str:
        return self.run(*args).stdout.strip()

    def blob(self, rev: str, path: str):
        """Return the bytes of `path` at `rev`, or None if it does not exist."""
        proc = self.run("show", f"{rev}:{path}", check=False, text=False)
        if proc.returncode != 0:
            return None
        return proc.stdout


def _repo_root(start: Path | None = None) -> Path:
    proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=str(start or Path.cwd()),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc.returncode != 0:
        raise PublishError("not inside a git working tree")
    return Path(proc.stdout.strip())


def _parse_updated(raw) -> datetime | None:
    """Extract the ISO-ish `"updated"` timestamp that export writes, if present."""
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    value = data.get("updated")
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def _validate_json(path: Path, raw: bytes) -> None:
    """Refuse to publish a truncated/corrupt dashboard payload."""
    try:
        json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise PublishError(f"{path} is not valid JSON ({exc}); refusing to publish it")


def _tracked_dirty_paths(git: Git) -> set[str]:
    """Paths with *tracked* modifications (untracked files survive reset --hard)."""
    dirty: set[str] = set()
    for line in git.run("status", "--porcelain").stdout.splitlines():
        if not line.strip() or line.startswith("??"):
            continue
        path = line[3:].strip()
        if " -> " in path:  # rename: keep the destination
            path = path.split(" -> ", 1)[1]
        dirty.add(path.strip('"'))
    return dirty


def publish(
    paths,
    *,
    branch: str = DEFAULT_BRANCH,
    remote: str = DEFAULT_REMOTE,
    message: str | None = None,
    attempts: int = DEFAULT_ATTEMPTS,
    retry_sleep: float = DEFAULT_RETRY_SLEEP,
    log=print,
    cwd: Path | None = None,
) -> int:
    """Publish `paths` (repo-relative) to `remote/branch`. Returns a process exit code.

    Exit codes: 0 = published, already current, or a newer snapshot is already on
    the branch; 1 = publication genuinely failed (nothing was force-pushed and the
    working tree / history are left intact).
    """
    root = _repo_root(cwd)
    git = Git(root)
    rel = [str(Path(p)) for p in paths]
    if not rel:
        raise PublishError("no files given to publish")

    snapshots: dict[str, bytes] = {}
    stamp = f"copybot {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}"
    for path in rel:
        target = root / path
        if not target.is_file():
            raise PublishError(f"missing file to publish: {path}")
        raw = target.read_bytes()
        if target.suffix == ".json":
            _validate_json(Path(path), raw)
        snapshots[path] = raw

    # Never throw away changes we were not asked to publish (paper-trader state,
    # DB files, edits in progress, ...). `reset --hard` below would lose them.
    unexpected = _tracked_dirty_paths(git) - set(rel)
    if unexpected:
        raise PublishError(
            "refusing to publish: unrelated tracked files have uncommitted changes "
            f"({', '.join(sorted(unexpected))}); they would be lost"
        )

    for attempt in range(1, attempts + 1):
        git.run("fetch", "--quiet", remote, branch)
        tip = git.out("rev-parse", "FETCH_HEAD")

        identical = True
        newer_on_remote: list[str] = []
        for path, raw in snapshots.items():
            remote_blob = git.blob(tip, path)
            if remote_blob == raw:
                continue
            identical = False
            remote_stamp = _parse_updated(remote_blob)
            local_stamp = _parse_updated(raw)
            if remote_stamp and local_stamp and remote_stamp > local_stamp:
                newer_on_remote.append(
                    f"{path} (remote {remote_stamp.isoformat()} > ours {local_stamp.isoformat()})"
                )

        if identical:
            log(f"publish: {remote}/{branch} already has this exact snapshot - nothing to publish")
            return 0

        if newer_on_remote:
            # A fresher run already published. Overwriting it would roll the
            # dashboard backwards, so this snapshot is simply retired.
            log(
                "publish: a newer dashboard snapshot is already on "
                f"{remote}/{branch} - leaving it in place: {'; '.join(newer_on_remote)}"
            )
            return 0

        # Rebase-free rebuild: put our snapshot directly on top of the current tip.
        git.run("reset", "--hard", "--quiet", tip)
        for path, raw in snapshots.items():
            (root / path).write_bytes(raw)
        git.run("add", "--", *rel)

        if git.run("diff", "--cached", "--quiet", check=False).returncode == 0:
            log("publish: snapshot matches the published dashboard - nothing to publish")
            return 0

        git.run("commit", "--quiet", "--message", message or stamp)
        sha = git.out("rev-parse", "--short", "HEAD")
        push = git.run("push", "--quiet", remote, f"HEAD:{branch}", check=False)
        if push.returncode == 0:
            log(f"publish: pushed {sha} to {remote}/{branch} (attempt {attempt}/{attempts})")
            return 0

        detail = (push.stderr or push.stdout or "").strip()
        log(
            f"publish: push attempt {attempt}/{attempts} did not land "
            f"({detail.splitlines()[-1] if detail else 'unknown error'}); re-reading {remote}/{branch}"
        )
        if attempt < attempts:
            time.sleep(retry_sleep)

    log(
        f"publish: FAILED to publish {', '.join(rel)} to {remote}/{branch} after "
        f"{attempts} attempts (no force-push was attempted)"
    )
    return 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Publish regenerated dashboard files to the default branch "
                    "without rebasing (newest snapshot wins).",
    )
    parser.add_argument("files", nargs="+", help="repository-relative files to publish")
    parser.add_argument("--branch", default=os.environ.get("PUBLISH_BRANCH", DEFAULT_BRANCH))
    parser.add_argument("--remote", default=os.environ.get("PUBLISH_REMOTE", DEFAULT_REMOTE))
    parser.add_argument("--message", default=None, help="commit message")
    parser.add_argument("--attempts", type=int, default=DEFAULT_ATTEMPTS)
    parser.add_argument("--retry-sleep", type=float, default=DEFAULT_RETRY_SLEEP)
    args = parser.parse_args(argv)

    try:
        return publish(
            args.files,
            branch=args.branch,
            remote=args.remote,
            message=args.message,
            attempts=args.attempts,
            retry_sleep=args.retry_sleep,
        )
    except PublishError as exc:
        print(f"::error::publish_dashboard: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
