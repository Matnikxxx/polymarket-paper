"""
Regression tests for publish_dashboard.py (the rebase-free dashboard publisher).

These tests reproduce the real production failure in miniature, entirely offline,
using throw-away git repositories in a temp directory:

  * A copybot run is checked out at an OLD commit, but origin/main has moved on
    with several full rewrites of docs/data.json and docs/quality.json.
    The old `git pull --rebase` + `git push` loop produced
    "CONFLICT (content): Merge conflict in docs/data.json" forever.
    The publisher must instead publish the new snapshot as a fast-forward, keep
    every old commit, and leave no rebase state behind.

  * Two writers racing for the same branch: the first push is rejected, the
    publisher re-reads origin/main and retries successfully without force-pushing
    and without dropping the other writer's commit.

  * An older snapshot must never overwrite a newer dashboard already on main.

  * Corrupt JSON or unrelated local edits must abort publishing (safety).

No network access, no real credentials, no trading data touched.
"""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import publish_dashboard  # noqa: E402


def git(cwd, *args, check=True):
    """Run git in `cwd` and return its CompletedProcess."""
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if check and proc.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed in {cwd}: {proc.stderr or proc.stdout}"
        )
    return proc


def dashboard(updated, marker="x"):
    return json.dumps({"updated": updated, "marker": marker}, sort_keys=True)


class PublishTestCase(unittest.TestCase):
    """Base fixture: a bare 'origin' repo, a writer clone and a shallow work clone."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.origin = self.tmp / "origin.git"
        git(self.tmp, "init", "--bare", "-q", "-b", "main", str(self.origin))

        # Seed the remote with a first dashboard commit.
        self.seed = self.tmp / "seed"
        git(self.tmp, "clone", "-q", self.origin.as_uri(), str(self.seed))
        self._git_identity(self.seed)
        self._write(self.seed, "docs/data.json", dashboard("2026-10-27T10:00:00+00:00", "seed"))
        self._write(self.seed, "docs/quality.json", dashboard("2026-10-27T10:00:00+00:00", "seed"))
        self._write(self.seed, "README.md", "paper trading\n")
        git(self.seed, "add", "-A")
        git(self.seed, "commit", "-q", "-m", "seed")
        git(self.seed, "push", "-q", "origin", "HEAD:refs/heads/main")

        # A second clone used to simulate *other* runs publishing to main.
        self.writer = self.tmp / "writer"
        git(self.tmp, "clone", "-q", self.origin.as_uri(), str(self.writer))
        self._git_identity(self.writer)

        # The workflow clone: depth 1, exactly like actions/checkout default.
        self.work = self.tmp / "work"
        git(self.tmp, "clone", "-q", "--depth", "1", self.origin.as_uri(), str(self.work))
        self._git_identity(self.work)

        # Mirrors actions/checkout's default `fetch-depth: 1` clone.
        self.assertEqual(
            "true", git(self.work, "rev-parse", "--is-shallow-repository").stdout.strip()
        )
        self.logs = []

    def tearDown(self):
        self._tmp.cleanup()

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _git_identity(repo):
        git(repo, "config", "user.name", "test-bot")
        git(repo, "config", "user.email", "bot@users.noreply.github.com")

    @staticmethod
    def _write(repo, relpath, content):
        target = Path(repo) / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def publish_remote_dashboard(self, updated, marker):
        """Simulate another copybot run committing a dashboard refresh to main."""
        git(self.writer, "fetch", "-q", "origin", "main")
        git(self.writer, "reset", "-q", "--hard", "origin/main")
        self._write(self.writer, "docs/data.json", dashboard(updated, marker))
        self._write(self.writer, "docs/quality.json", dashboard(updated, marker))
        git(self.writer, "add", "-A")
        git(self.writer, "commit", "-q", "-m", f"copybot {updated}")
        git(self.writer, "push", "-q", "origin", "HEAD:refs/heads/main")

    def remote_file(self, relpath):
        return git(self.origin, "show", f"main:{relpath}").stdout

    def remote_tip(self):
        return git(self.origin, "rev-parse", "main").stdout.strip()

    def remote_commit_count(self, branch="main"):
        return int(git(self.origin, "rev-list", "--count", branch).stdout.strip())

    def publish(self, files=("docs/data.json", "docs/quality.json"), **kwargs):
        kwargs.setdefault("attempts", 3)
        kwargs.setdefault("retry_sleep", 0)
        kwargs.setdefault("log", self.logs.append)
        return publish_dashboard.publish(list(files), cwd=self.work, **kwargs)

    def assert_clean_worktree(self):
        status = git(self.work, "status", "--porcelain").stdout.strip()
        self.assertEqual("", status, "working tree should be clean after publishing")
        self.assertFalse((self.work / ".git" / "rebase-merge").exists())
        self.assertFalse((self.work / ".git" / "rebase-apply").exists())


class TestStaleCheckoutPublishing(PublishTestCase):
    """The exact production bug: publishing from an out-of-date queued run."""

    def test_publishes_fast_forward_when_remote_moved_ahead(self):
        old_tip = self.remote_tip()

        # origin/main advances three times while our run sat in the queue.
        for i, stamp in enumerate(
            ["2026-10-27T11:00:00+00:00", "2026-10-27T12:00:00+00:00", "2026-10-27T13:00:00+00:00"]
        ):
            self.publish_remote_dashboard(stamp, f"other-run-{i}")

        # ...and our (stale) checkout now exports a fresher dashboard.
        ours = dashboard("2026-10-27T14:00:00+00:00", "our-run")
        self._write(self.work, "docs/data.json", ours)
        self._write(self.work, "docs/quality.json", ours)
        tip_before_publish = self.remote_tip()
        commits_before = self.remote_commit_count()

        self.assertEqual(0, self.publish())

        # Our snapshot is live...
        self.assertEqual(ours, self.remote_file("docs/data.json"))
        self.assertEqual(ours, self.remote_file("docs/quality.json"))

        # ...history grew by exactly one commit, and the old tip is still an
        # ancestor (nothing was discarded or force-pushed).
        self.assertEqual(commits_before + 1, self.remote_commit_count())
        self.assertEqual(0, git(self.origin, "merge-base", "--is-ancestor", old_tip, "main").returncode)
        self.assertEqual(
            "0",
            git(self.origin, "rev-list", "--count", "--merges", "main").stdout.strip(),
            "publishing must not create merge commits",
        )
        self.assertEqual(1, len(git(self.origin, "rev-list", "--max-parents=0", "main").stdout.split()))

        # Our new commit sits directly on top of the previous remote tip
        # (fast-forward publish, no merge commit).
        self.assertEqual(
            tip_before_publish, git(self.origin, "rev-parse", "main^").stdout.strip()
        )
        self.assert_clean_worktree()

    def test_no_change_needed_when_remote_already_matches(self):
        ours = self.remote_file("docs/data.json")
        self._write(self.work, "docs/data.json", ours)
        self._write(self.work, "docs/quality.json", self.remote_file("docs/quality.json"))
        commits_before = self.remote_commit_count()

        self.assertEqual(0, self.publish())
        self.assertEqual(commits_before, self.remote_commit_count())
        self.assertIn("nothing to publish", "\n".join(self.logs))

    def test_older_snapshot_never_overwrites_newer_dashboard(self):
        self.publish_remote_dashboard("2026-10-27T15:00:00+00:00", "fresher")
        fresher = self.remote_file("docs/data.json")
        tip_before = self.remote_tip()

        stale = dashboard("2026-10-27T09:00:00+00:00", "stale-run")
        self._write(self.work, "docs/data.json", stale)
        self._write(self.work, "docs/quality.json", stale)

        self.assertEqual(0, self.publish())
        self.assertEqual(fresher, self.remote_file("docs/data.json"))
        self.assertEqual(tip_before, self.remote_tip())
        self.assertIn("newer dashboard snapshot", "\n".join(self.logs))


class TestRootCauseDocumented(PublishTestCase):
    """Pin the production failure mode so nobody reintroduces rebase publishing."""

    def test_old_rebase_strategy_conflicts_and_new_publisher_recovers(self):
        # Three other copybot runs published full rewrites while this run queued.
        for i in range(3):
            self.publish_remote_dashboard(f"2026-10-27T1{i}:00:00+00:00", f"other-{i}")

        ours = dashboard("2026-10-27T14:00:00+00:00", "ours")
        self._write(self.work, "docs/data.json", ours)
        self._write(self.work, "docs/quality.json", ours)
        git(self.work, "add", "docs/data.json", "docs/quality.json")
        git(self.work, "commit", "-q", "-m", "copybot old-style commit")

        # The old flush(): commit, then `git pull --rebase --autostash origin main`.
        pull = git(
            self.work, "pull", "--rebase", "--autostash", "origin", "main", check=False
        )
        combined = pull.stdout + pull.stderr
        self.assertNotEqual(0, pull.returncode, "the old strategy must fail in this state")
        self.assertIn("CONFLICT (content): Merge conflict in docs/data.json", combined)
        self.assertIn("CONFLICT (content): Merge conflict in docs/quality.json", combined)
        self.assertIn("could not apply", combined)
        git(self.work, "rebase", "--abort", check=False)

        # The publisher handles the same state in one attempt, no conflicts.
        self.assertEqual(0, self.publish(attempts=1))
        self.assertEqual(ours, self.remote_file("docs/data.json"))
        self.assert_clean_worktree()


class TestConcurrentWriter(PublishTestCase):
    def test_retries_when_another_writer_wins_the_push_race(self):
        # Another run has a commit ready that it will push mid-flight.
        git(self.writer, "fetch", "-q", "origin", "main")
        git(self.writer, "reset", "-q", "--hard", "origin/main")
        self._write(
            self.writer, "docs/data.json", dashboard("2026-10-27T13:00:00+00:00", "racer")
        )
        git(self.writer, "add", "-A")
        git(self.writer, "commit", "-q", "-m", "racer")

        racer = git(self.writer, "rev-parse", "HEAD").stdout.strip()

        # pre-push hook: on the first push attempt another writer lands its commit
        # first, so our push is rejected as non-fast-forward.
        flag = self.tmp / "race_done"
        hook = self.work / ".git" / "hooks" / "pre-push"
        hook.write_text(
            textwrap.dedent(
                f"""\
                #!/bin/sh
                if [ ! -f "{flag}" ]; then
                  : > "{flag}"
                  git -C "{self.writer}" push -q origin HEAD:refs/heads/main
                fi
                exit 0
                """
            )
        )
        hook.chmod(0o755)

        ours = dashboard("2026-10-27T14:00:00+00:00", "ours")
        self._write(self.work, "docs/data.json", ours)
        self._write(self.work, "docs/quality.json", ours)

        self.assertEqual(0, self.publish(attempts=3))
        self.assertEqual(ours, self.remote_file("docs/data.json"))
        # The competing commit survived; our commit was rebased on top of it.
        self.assertEqual(
            0, git(self.origin, "merge-base", "--is-ancestor", racer, "main").returncode
        )
        self.assertEqual(
            racer, git(self.origin, "rev-parse", "main^").stdout.strip()
        )
        self.assert_clean_worktree()


class TestSafetyGuards(PublishTestCase):
    def test_refuses_to_publish_invalid_json(self):
        self._write(self.work, "docs/data.json", '{"updated": "2026-10-27T14:00:00+00:00"')
        self._write(self.work, "docs/quality.json", dashboard("2026-10-27T14:00:00+00:00"))
        tip_before = self.remote_tip()
        with self.assertRaises(publish_dashboard.PublishError):
            self.publish()
        self.assertEqual(tip_before, self.remote_tip())

    def test_refuses_to_reset_over_unrelated_local_changes(self):
        self._write(self.work, "README.md", "locally edited notes\n")
        ours = dashboard("2026-10-27T14:00:00+00:00", "ours")
        self._write(self.work, "docs/data.json", ours)
        self._write(self.work, "docs/quality.json", ours)

        with self.assertRaises(publish_dashboard.PublishError) as ctx:
            self.publish()
        self.assertIn("README.md", str(ctx.exception))
        self.assertEqual("locally edited notes\n", (self.work / "README.md").read_text())

    def test_never_force_pushes(self):
        """Publishing must always be a fast-forward (no --force / --force-with-lease)."""
        source = (REPO_ROOT / "publish_dashboard.py").read_text()
        self.assertNotIn("--force", source)
        self.assertNotIn("push --mirror", source)

    def test_cli_returns_nonzero_on_missing_file(self):
        self.assertEqual(1, publish_dashboard.main(["--attempts", "1", "docs/nope.json"]))


class TestWorkflowWiring(unittest.TestCase):
    """Guard the workflow changes that coordinate writers on main."""

    def _workflow(self, name):
        return (REPO_ROOT / ".github" / "workflows" / name).read_text()

    def test_all_main_writers_share_one_concurrency_group(self):
        for name in ("copybot.yml", "tick.yml"):
            body = self._workflow(name)
            self.assertIn("group: main-writer", body, f"{name} must join the shared group")
            self.assertNotIn("group: copybot", body)
            self.assertNotIn("group: tick", body)

    def test_copybot_publishes_without_rebasing(self):
        body = self._workflow("copybot.yml")
        self.assertNotIn("pull --rebase", body)
        self.assertNotIn("rebase --abort", body)
        self.assertIn("publish_dashboard.py docs/data.json docs/quality.json", body)

    def test_tick_stages_files_before_checking_for_changes(self):
        body = self._workflow("tick.yml")
        self.assertNotIn("pull --rebase", body)
        self.assertIn("git add", body)
        self.assertIn("publish_dashboard.py", body)


if __name__ == "__main__":
    unittest.main()
