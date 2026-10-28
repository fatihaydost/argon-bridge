import os
import subprocess
import time
import unittest

from argon_bridge import config, jobs, state
from tests.helpers import Env


def git(root, *args):
    return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True).stdout.strip()


def write(root, rel, text):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(text)


def phase(title):
    return {"title": title, "goal": "g", "check": {"command": "python3 -m unittest discover -s tests", "expect": "OK"}}


class JobsTest(Env):
    def setUp(self):
        super().setUp()
        self.root = self.git_repo()

    def test_create_makes_branch_and_worktree_outside_user_folder(self):
        job = jobs.create("s", self.root, "turkish-slug", [phase("one")], [], [])
        self.assertTrue(os.path.isdir(job["worktree"]))
        self.assertFalse(job["worktree"].startswith(self.root))
        self.assertEqual(git(job["worktree"], "rev-parse", "--abbrev-ref", "HEAD"), job["branch"])
        self.assertEqual(git(self.root, "status", "--porcelain"), "")
        self.assertEqual(jobs.load(job["id"])["phases"][0]["status"], "pending")
        self.assertEqual([j["id"] for j in jobs.all()], [job["id"]])

    def test_session_pruning_never_touches_a_worktree(self):
        write(self.root, "app/sessions/store.py", "x = 1\n")
        git(self.root, "add", ".")
        git(self.root, "commit", "-qm", "sessions")
        job = jobs.create("s", self.root, "old", [phase("one")], [], [])
        f = os.path.join(job["worktree"], "app", "sessions", "store.py")
        old = time.time() - (state.SESSION_DAYS + 1) * 86400  # a job paused for weeks
        os.utime(f, (old, old))
        state.append_prompt("new-session", "hi")  # the first prompt of a new session prunes
        self.assertTrue(os.path.exists(f))
        self.assertEqual(git(job["worktree"], "status", "--porcelain"), "")

    def test_same_name_twice_gets_two_jobs(self):
        a = jobs.create("s", self.root, "x", [phase("one")], [], [])
        b = jobs.create("s", self.root, "x", [phase("one")], [], [])
        self.assertNotEqual(a["id"], b["id"])

    def test_private_repo_refused(self):
        config.set_value("private_roots", [self.root])
        with self.assertRaises(ValueError):
            jobs.create("s", self.root, "x", [phase("one")], [], [])

    def test_not_a_repo_refused(self):
        with self.assertRaises(ValueError):
            jobs.create("s", self.tmp, "x", [phase("one")], [], [])

    def test_remove_worktree_deletes_branch(self):
        job = jobs.create("s", self.root, "x", [phase("one")], [], [])
        jobs.remove_worktree(job)
        self.assertFalse(os.path.exists(job["worktree"]))
        self.assertNotIn(job["branch"], git(self.root, "branch", "--list", job["branch"]))

    def test_prepare_links_env_and_deps_without_committing_them(self):
        write(self.root, ".env", "SECRET=1\n")
        os.makedirs(os.path.join(self.root, "node_modules", "a"))
        job = jobs.create("s", self.root, "x", [phase("one")], [], [])
        linked = jobs.prepare(job, [".env", "node_modules", "missing"])
        self.assertEqual(linked, [".env", "node_modules"])
        with open(os.path.join(job["worktree"], ".env")) as f:  # a symlink, or on Windows maybe a copy
            self.assertEqual(f.read(), "SECRET=1\n")
        self.assertEqual(git(job["worktree"], "status", "--porcelain"), "")
        self.assertEqual(jobs.prepare(job, [".env"]), [])  # already there
        jobs.remove_worktree(jobs.load(job["id"]))
        with open(os.path.join(self.root, ".git", "info", "exclude")) as f:
            self.assertNotIn("/.env", f.read())  # the user's repo keeps no trace
        # removing the worktree never deletes through a link (a Windows junction would be walked otherwise)
        self.assertTrue(os.path.isdir(os.path.join(self.root, "node_modules", "a")))
        self.assertTrue(os.path.isfile(os.path.join(self.root, ".env")))

    def test_prepare_refuses_paths_outside_the_repo(self):
        job = jobs.create("s", self.root, "x", [phase("one")], [], [])
        self.assertEqual(jobs.prepare(job, ["../st", "/etc"]), [])

    def test_prepare_normalizes_paths_and_keeps_links_inside(self):
        write(self.root, ".env", "S=1\n")
        job = jobs.create("s", self.root, "x", [phase("one")], [], [])
        name = os.path.basename(self.root)
        self.assertEqual(jobs.prepare(job, [f"../{name}/.env", "./.env"]), [".env"])
        self.assertEqual(git(job["worktree"], "status", "--porcelain"), "")

    def test_exclude_keeps_the_users_last_line_intact(self):
        job = jobs.create("s", self.root, "x", [phase("one")], [], [])
        path = os.path.join(self.root, ".git", "info", "exclude")
        with open(path, "w") as f:
            f.write("/mine")  # no final newline
        jobs.exclude(job, ["b"])
        jobs.remove_worktree(jobs.load(job["id"]))
        with open(path) as f:
            self.assertEqual(f.read().splitlines(), ["/mine"])

    def test_two_jobs_on_one_repo_keep_their_shared_exclude_line(self):
        a = jobs.create("s", self.root, "a", [phase("one")], [], [])
        b = jobs.create("s", self.root, "b", [phase("one")], [], [])
        for job in (a, b):
            os.makedirs(os.path.join(job["worktree"], ".agents", "skills", "p"))
            write(job["worktree"], ".agents/skills/p/SKILL.md", "x")
            jobs.exclude(job, [".agents/skills/p"])
        jobs.remove_worktree(jobs.load(a["id"]))
        a = jobs.load(a["id"])
        a["state"] = "rejected"
        jobs.save(a)
        self.assertEqual(git(b["worktree"], "status", "--porcelain"), "")
        jobs.remove_worktree(jobs.load(b["id"]))
        with open(os.path.join(self.root, ".git", "info", "exclude")) as f:
            self.assertNotIn("/.agents/skills/p", f.read())


if __name__ == "__main__":
    unittest.main()
