"""Parallel Claude sessions in one checkout: each reviews only its own commits and edits (24.10.2026)."""
import json
import os
import subprocess

from argon_bridge import config, hooks, state
from tests.helpers import Env, success

FAIL = {"status": "ERROR", "error": "deadline exceeded"}


def put(root, files):
    for rel, text in files.items():
        with open(os.path.join(root, rel), "w") as f:
            f.write(text)


def git(root, *args):
    return subprocess.run(["git", "-C", root, *args], check=True, capture_output=True, text=True).stdout.strip()


class IsolationTest(Env):
    def setUp(self):
        super().setUp()
        self.root = self.git_repo()
        for sid in ("X", "U"):
            hooks.on_prompt({"session_id": sid, "prompt": f"work of {sid}"})

    def inp(self, sid, command="git commit -m x"):
        return {"session_id": sid, "cwd": self.root, "tool_input": {"command": command}}

    def pre(self, sid, files, command=None):
        put(self.root, files)
        hooks.on_commit(self.inp(sid, command or f"git add {' '.join(files)} && git commit -m {sid}"))

    def land(self, sid, files, msg=None):
        git(self.root, "add", *files)
        git(self.root, "commit", "-qm", msg or sid)
        return git(self.root, "rev-parse", "HEAD")

    def post(self, sid, command=None):
        return hooks.on_commit_done(self.inp(sid, command or "git commit -m x"))

    def commit(self, sid, files):
        self.pre(sid, files)
        sha = self.land(sid, files)
        self.out = self.post(sid)
        return sha

    def edit(self, sid, files):
        put(self.root, files)
        for rel in files:
            hooks.on_file_edit({"session_id": sid, "cwd": self.root, "tool_name": "Write",
                                "tool_input": {"file_path": os.path.join(self.root, rel)}})

    # (a)
    def test_a_failed_review_stays_with_its_session(self):
        self.agy_returns(FAIL, success({"findings": []}), success({"findings": []}))
        self.commit("X", {"x1.py": "x = 1\n" * 40, "x2.py": "x = 2\n" * 40})
        self.assertIn("deadline", json.dumps(self.out))  # the failure reached X
        self.commit("U", {"u1.py": "u = 1\n" * 40, "u2.py": "u = 2\n" * 40})
        self.assertIn("u1.py", self.last_packet())
        self.assertNotIn("x1.py", self.last_packet())  # X's change never enters U's review
        self.commit("X", {"x3.py": "x = 3\n" * 40})
        self.assertIn("x1.py", self.last_packet())  # it goes into X's next review
        self.assertIn("x3.py", self.last_packet())
        self.assertNotIn("u1.py", self.last_packet())
        self.assertEqual(len(self.agy_calls()), 3)

    # (b)
    def test_every_commit_of_a_chained_command_is_reviewed(self):
        self.agy_returns(success({"findings": []}))
        cmd = "git add a1.py && git commit -m a && git add b1.py && git commit -m b"
        self.pre("X", {"a1.py": "a = 1\n" * 40, "b1.py": "b = 1\n" * 40}, cmd)
        first = self.land("X", ["a1.py"], "a")
        second = self.land("X", ["b1.py"], "b")
        ctx = self.post("X", cmd)["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(len(self.agy_calls()), 1)
        self.assertIn("a1.py", self.last_packet())
        self.assertIn("b1.py", self.last_packet())
        self.assertIn(first[:7], ctx)
        self.assertIn(second[:7], ctx)

    # (c)
    def test_another_sessions_uncommitted_file_stays_out_of_stop(self):
        config.set_value("mode", "thorough")
        self.agy_returns(success({"findings": []}))
        self.edit("X", {"x1.py": "x = 1\n" * 40, "x2.py": "x = 2\n" * 40})
        self.edit("U", {"u1.py": "u = 1\n" * 40, "u2.py": "u = 2\n" * 40})
        hooks.on_stop({"session_id": "U", "cwd": self.root})
        self.assertEqual(len(self.agy_calls()), 1)
        self.assertIn("u1.py", self.last_packet())
        self.assertNotIn("x1.py", self.last_packet())

    def test_stop_without_own_edits_or_commits_reviews_nothing(self):
        config.set_value("mode", "thorough")
        self.edit("X", {"x1.py": "x = 1\n" * 40, "x2.py": "x = 2\n" * 40})
        self.assertEqual(hooks.on_stop({"session_id": "U", "cwd": self.root}), {})
        self.assertEqual(self.agy_calls(), [])

    def test_committed_paths_leave_the_touched_list(self):
        self.agy_returns(success({"findings": []}))
        self.edit("X", {"x1.py": "x = 1\n" * 40, "x2.py": "x = 2\n"})
        self.pre("X", {"x1.py": "x = 1\n" * 40}, "git add x1.py && git commit -m x")
        self.land("X", ["x1.py"])
        self.post("X")
        self.assertEqual(state.session("X")["touched"][self.root], ["x2.py"])

    # (d)
    def test_a_commit_inside_another_window_belongs_to_its_author(self):
        self.agy_returns(success({"findings": []}))
        big_x, big_u = {"x1.py": "x = 1\n" * 40, "x2.py": "x\n" * 40}, {"u1.py": "u = 1\n" * 40, "u2.py": "u\n" * 40}
        self.pre("X", big_x)
        self.pre("U", big_u)
        u = self.land("U", list(big_u))
        x = self.land("X", list(big_x))
        self.post("X")  # U is still inside its own commit
        self.assertNotIn("u1.py", self.last_packet())
        self.assertIn("x1.py", self.last_packet())
        self.post("U")
        self.assertIn("u1.py", self.last_packet())
        self.assertNotIn("x1.py", self.last_packet())
        with state.owners(self.root) as o:
            self.assertEqual((o["ledger"][x]["sid"], o["ledger"][u]["sid"]), ("X", "U"))
            self.assertEqual(o["inflight"], {})
        self.assertEqual(len(self.agy_calls()), 2)

    def test_the_first_to_finish_leaves_the_other_commit(self):
        self.agy_returns(success({"findings": []}))
        big_x, big_u = {"x1.py": "x = 1\n" * 40, "x2.py": "x\n" * 40}, {"u1.py": "u = 1\n" * 40, "u2.py": "u\n" * 40}
        self.pre("X", big_x)
        self.pre("U", big_u)
        self.land("U", list(big_u))
        self.post("U")  # X is still inside its commit: only U's own commit is U's
        self.assertIn("u1.py", self.last_packet())
        self.land("X", list(big_x))
        self.post("X")
        self.assertIn("x1.py", self.last_packet())
        self.assertNotIn("u1.py", self.last_packet())

    def test_unknown_paths_claim_and_mark_ambiguous(self):
        self.agy_returns(success({"findings": []}))
        self.pre("U", {"u1.py": "u = 1\n" * 40})
        put(self.root, {"x1.py": "x = 1\n" * 40, "x2.py": "x\n" * 40})
        git(self.root, "add", "x1.py", "x2.py")
        hooks.on_commit(self.inp("X", "git commit -m x x1.py x2.py"))  # nothing new staged by the command itself
        x = self.land("X", [])
        self.post("X")
        with state.owners(self.root) as o:
            self.assertEqual(o["ledger"][x]["sid"], "X")

    def test_stale_inflight_is_dropped(self):
        with state.owners(self.root) as o:
            o["inflight"]["gone"] = {"head": None, "paths": ["x1.py"], "t": 0}
        self.agy_returns(success({"findings": []}))
        x = self.commit("X", {"x1.py": "x = 1\n" * 40, "x2.py": "x\n" * 40})
        with state.owners(self.root) as o:
            self.assertEqual(o["inflight"], {})
            self.assertEqual(o["ledger"][x]["sid"], "X")

    # (e)
    def test_log_line_names_session_and_commits(self):
        self.agy_returns(success({"findings": []}))
        x = self.commit("X", {"x1.py": "x = 1\n" * 40, "x2.py": "x\n" * 40})
        line = state.recent_log(1)[0]
        self.assertEqual((line["sid"], line["root"], line["shas"], line["head"]), ("X", self.root, [x], x))
        self.assertTrue(line["base"])

    def test_legacy_repo_baseline_seeds_the_first_session(self):
        base = git(self.root, "rev-parse", "HEAD")
        put(self.root, {"old.py": "o = 1\n" * 40})
        git(self.root, "add", "old.py")
        git(self.root, "commit", "-qm", "old")
        state.save_repo(self.root, {"last_reviewed_head": base})
        self.agy_returns(success({"findings": []}))
        self.commit("X", {"x1.py": "x = 1\n" * 40})
        self.assertIn("old.py", self.last_packet())
        self.assertNotIn("last_reviewed_head", state.repo(self.root))
        self.commit("U", {"u1.py": "u = 1\n" * 40, "u2.py": "u\n" * 40})
        self.assertNotIn("old.py", self.last_packet())

    def test_dead_review_returns_its_commits_to_its_session(self):
        orig = hooks.run_job
        def dies(path):
            with open(path) as f:
                state.mark_running("dead", hooks._marker(json.load(f)), pid=999999)
        hooks.run_job = dies
        try:
            x = self.commit("X", {"x1.py": "x = 1\n" * 40, "x2.py": "x\n" * 40})
        finally:
            hooks.run_job = orig
        self.assertIn("background check died", json.dumps(self.out))
        self.assertEqual(state.session("X")["code"][self.root]["pending"], [x])
        self.assertNotIn(self.root, state.session("U").get("code", {}))
