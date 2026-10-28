import io
import json
import os
import subprocess
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout

from argon_bridge import cli, framing, gateway, hooks, state
from tests.helpers import Env, finding, run_cli_output, success

BIG = {"a.py": "x = 1\n" * 25, "b.py": "y = 2\n" * 25}


class VisibilityTest(Env):
    def setUp(self):
        super().setUp()
        self.root = self.git_repo()
        hooks.on_prompt({"session_id": "s", "prompt": "make add() add"})

    def commit(self, files=BIG, sid="s"):
        for rel, text in files.items():
            with open(os.path.join(self.root, rel), "w") as f:
                f.write(text)
        subprocess.run(["git", "-C", self.root, "add", "."], check=True)
        inp = {"session_id": sid, "cwd": self.root, "tool_input": {"command": "git commit -m x"}}
        hooks.on_commit(inp)
        subprocess.run(["git", "-C", self.root, "commit", "-qm", "x"], check=True)
        return hooks.on_commit_done(inp)

    def result(self, findings, shas, kind="code"):
        state.inbox_put("s", {"kind": kind, "root": self.root, "status": "Argon: n findings (1s)", "ok": True,
                              "findings": findings, "shas": shas, "range": None})

    # 13
    def test_background_note_says_fixes_need_approval(self):
        specs, orig = [], hooks.run_job
        hooks.run_job = specs.append
        try:
            ctx = self.commit()["hookSpecificOutput"]["additionalContext"]
        finally:
            hooks.run_job = orig
        self.assertIn("fixes need the user's approval", ctx)
        self.assertNotIn("go into a new commit", ctx)

    # 26
    def test_a_detail_repeating_the_reason_is_not_printed(self):
        line = framing.status_line(gateway.Result(False, error="malformed",
                                                  detail="answer did not follow the required form"))
        self.assertEqual(line.count("required form"), 1)
        line = framing.status_line(gateway.Result(False, error="timeout", detail="no answer within 400s"))
        self.assertIn("no answer within 400s", line)

    # 12
    def test_a_finding_in_two_reviews_is_printed_once_with_both_shas(self):
        same = finding(claim="total() sums None")
        self.result([same, finding(claim="other", line=9)], ["a" * 40, "b" * 40])
        self.result([dict(same, claim="  Total() sums  none "), finding(claim="third", line=3)], ["c" * 40])
        text, has = hooks._inbox("s")
        self.assertTrue(has)
        self.assertEqual(text.lower().count("total() sums"), 1)
        self.assertIn("2 commits reviewed: aaaaaaa bbbbbbb", text)
        self.assertIn("1 commit reviewed: ccccccc", text)
        self.assertIn('"also_in_review_of": "ccccccc"', text)
        self.assertIn("third", text)
        self.assertEqual(text.count("UNTRUSTED CLAIMS"), 1)  # one protocol for every review

    # 22
    def test_counter_names_reviewed_and_not_yet_reviewed_commits(self):
        data = state.session("s")
        data.setdefault("code", {})[self.root] = {"pending": ["d" * 40, "e" * 40]}
        state.save_session("s", data)
        self.result([], ["f" * 40])
        text, _ = hooks._inbox("s")
        self.assertIn("1 commit reviewed: fffffff", text)
        self.assertIn("2 commits not reviewed yet (will be in the next review)", text)

    def test_long_findings_are_cut_and_the_rest_written_to_a_file(self):
        many = [finding(claim=f"claim {i} " + "x" * 250, line=i, evidence="e" * 390) for i in range(8)]
        for n in range(6):
            self.result([dict(f, line=f["line"] + 100 * n) for f in many], [f"{n}" * 40])
        text, _ = hooks._inbox("s")
        self.assertLessEqual(len(text), hooks.MAX_CONTEXT)
        tail = text.rstrip().splitlines()[-1]
        self.assertRegex(tail, r"\d+ more findings in ")
        path = tail.split(" more findings in ", 1)[1].strip()
        with open(path) as f:
            full = f.read()
        self.assertIn("claim 7", full)
        self.assertEqual(full.count('"claim"'), 48)

    # 25
    def test_status_counts_waiting_results_and_running_jobs(self):
        state.inbox_put("abcdef1234", {"kind": "code", "root": self.root, "status": "x", "findings": None})
        state.inbox_put("abcdef1234", {"kind": "code", "root": self.root, "status": "y", "findings": None})
        state.mark_running("j1", {"sid": "s9876543210", "kind": "code", "root": self.root, "timeout": 900})
        out = run_cli_output("status")
        self.assertIn("waiting results: abcdef12 2", out)
        self.assertRegex(out, r"running: code s9876543 in .*, \d+ s")

    # 12, wake: asyncRewake
    def test_wake_hands_over_a_review_that_finishes_later(self):
        state.mark_running("j1", {"sid": "s", "kind": "code", "root": self.root, "timeout": 900})

        def finish():
            time.sleep(0.5)
            self.result([finding(claim="late bug")], ["a" * 40])
            state.clear_running("j1")
        threading.Thread(target=finish).start()
        inp = {"session_id": "s", "cwd": self.root, "tool_input": {"command": "git commit -m x"}}
        text = hooks.wake(inp, grace=5, poll=0.1)
        self.assertIn("late bug", text)
        self.assertEqual(hooks._inbox("s"), ("", False))  # handed over once

    def test_wake_ignores_other_commands_and_a_second_waiter(self):
        inp = {"session_id": "s", "cwd": self.root, "tool_input": {"command": "ls"}}
        self.assertIsNone(hooks.wake(inp, grace=5, poll=0.1))
        lock = os.path.join(state.dir(), "wake", "s")
        os.makedirs(os.path.dirname(lock))
        with open(lock, "w") as f:
            f.write(str(os.getpid()))
        self.result([finding()], ["a" * 40])
        inp["tool_input"]["command"] = "git commit -m x"
        t = time.time()
        self.assertIsNone(hooks.wake(inp, grace=5, poll=0.1))
        self.assertLess(time.time() - t, 1)

    def test_wake_hook_exits_2_with_the_result_on_stderr(self):
        self.result([finding(claim="woken")], ["a" * 40])
        inp = {"session_id": "s", "cwd": self.root, "tool_input": {"command": "git commit -m x"}}
        err, out = io.StringIO(), io.StringIO()
        import sys
        stdin = sys.stdin
        sys.stdin = io.StringIO(json.dumps(inp))
        try:
            with redirect_stderr(err), redirect_stdout(out):
                code = cli.main(["hook-review-wake"])
        finally:
            sys.stdin = stdin
        self.assertEqual(code, 2)
        self.assertIn("woken", err.getvalue())

    def test_wake_hook_is_registered_as_async_rewake(self):
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hooks",
                               "hooks.json")) as f:
            post = json.load(f)["hooks"]["PostToolUse"]
        wake = [h for m in post for h in m["hooks"] if h["command"].endswith("hook-review-wake")]
        self.assertEqual(len(wake), 1)
        self.assertTrue(wake[0]["asyncRewake"])
        self.assertGreater(wake[0]["timeout"], 2 * 1800 + 60)


if __name__ == "__main__":
    unittest.main()
