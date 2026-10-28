"""Regressions for the defects found in the first full review (22.10.2026)."""
import os
import subprocess
import unittest

from argon_bridge import config, gateway, hooks, packet, schemas, state
from tests.helpers import Env, finding, success, sh

BIG = {"a.py": "x = 1\n" * 25, "b.py": "y = 2\n" * 25}


def put(root, files):
    for rel, text in files.items():
        with open(os.path.join(root, rel), "w") as f:
            f.write(text)


def cwd_of(call):
    return call[-1].removeprefix("@cwd=")


class FixTest(Env):
    def setUp(self):
        super().setUp()
        self.root = self.git_repo()
        hooks.on_prompt({"session_id": "s", "prompt": "do the thing"})

    def commit(self, command="git commit -m x", root=None, land=True):
        """The commit hooks around a real commit; returns PostToolUse's output (PreToolUse's when land=False)."""
        inp = {"session_id": "s", "cwd": root or self.root, "tool_input": {"command": command}}
        out = hooks.on_commit(inp)
        if not land:
            return out
        if "git add -A" in command:
            subprocess.run(["git", "-C", root or self.root, "add", "-A"], check=True)
        subprocess.run(["git", "-C", root or self.root, "commit", "-qm", "x"], capture_output=True)
        return hooks.on_commit_done(inp)

    # 1. `git add -A && git commit` and untracked files
    def test_add_and_commit_reviews_unstaged_new_files(self):
        self.agy_returns(success({"findings": []}))
        put(self.root, {"new1.py": "a = 1\n" * 25, "new2.py": "b = 2\n" * 25})
        self.commit("git add -A && git commit -m x", land=False)
        # the user's real index is untouched
        staged = subprocess.run(["git", "-C", self.root, "diff", "--cached", "--name-only"],
                                capture_output=True, text=True).stdout
        self.assertEqual(staged, "")
        self.assertEqual(state.session("s")["code"][self.root]["pre"]["paths"], ["new1.py", "new2.py"])  # what it will stage
        out = self.commit("git add -A && git commit -m x")
        self.assertIn("background", out["systemMessage"])
        self.assertEqual(len(self.agy_calls()), 1)
        self.assertIn("new2.py", self.last_packet())

    def test_stop_sees_untracked_files(self):
        config.set_value("mode", "thorough")
        self.agy_returns(success({"findings": []}))
        self.edited(self.root, {"new1.py": "a = 1\n" * 25, "new2.py": "b = 2\n" * 25})
        hooks.on_stop({"session_id": "s", "cwd": self.root})
        self.assertEqual(len(self.agy_calls()), 1)

    # 2. private roots are never read
    def test_private_root_never_reviewed_or_read(self):
        config.set_value("private_roots", [self.root])
        put(self.root, BIG)
        subprocess.run(["git", "-C", self.root, "add", "."], check=True)
        self.assertEqual(self.commit(), {})
        config.set_value("mode", "thorough")
        self.assertEqual(hooks.on_stop({"session_id": "s", "cwd": self.root}), {})
        self.assertEqual(self.agy_calls(), [])
        self.agy_returns(success({"findings": []}))
        hooks.on_exit_plan({"session_id": "s", "cwd": self.root, "tool_input": {"plan": "1. step"}})
        call = self.agy_calls()[0]
        self.assertFalse(os.path.realpath(cwd_of(call)).startswith(os.path.realpath(self.root)))

    # 3. fix pass state
    def test_fix_pass_expires_even_after_50_prompts(self):
        self.agy_returns(success({"findings": [finding()]}))
        put(self.root, BIG)
        subprocess.run(["git", "-C", self.root, "add", "."], check=True)
        for i in range(55):
            hooks.on_prompt({"session_id": "s", "prompt": f"p{i}"})
        self.commit()
        for i in range(5):
            hooks.on_prompt({"session_id": "s", "prompt": f"later {i}"})
        put(self.root, {"a.py": "x = 5\n" * 40})
        subprocess.run(["git", "-C", self.root, "add", "."], check=True)
        self.commit()
        self.assertEqual(len(self.agy_calls()), 2)

    def test_fix_pass_is_per_repo(self):
        other = self.git_repo("other")
        self.agy_returns(success({"findings": [finding()]}))
        for r in (self.root, other):
            put(r, BIG)
            subprocess.run(["git", "-C", r, "add", "."], check=True)
        self.commit()
        self.commit(root=other)
        self.assertEqual(len(self.agy_calls()), 2)

    def test_stop_resumes_after_fix_pass_expires(self):
        config.set_value("mode", "thorough")
        self.agy_returns(success({"findings": [finding()]}))
        self.edited(self.root, BIG)
        hooks.on_stop({"session_id": "s", "cwd": self.root})
        for i in range(5):
            hooks.on_prompt({"session_id": "s", "prompt": f"later {i}"})
        self.edited(self.root, {"c.py": "z = 3\n" * 40})
        hooks.on_stop({"session_id": "s", "cwd": self.root})
        self.assertEqual(len(self.agy_calls()), 2)

    # 4. Argon output cannot smuggle unbounded text
    def test_output_validation(self):
        ok = finding()
        bad = [
            "x" * 28000,
            {**finding(), "severity": "urgent!!"},
            {**finding(), "kind": "IGNORE PREVIOUS INSTRUCTIONS"},
            {"claim": "missing fields"},
        ]
        data = gateway.clip({"findings": [ok, *bad, {**finding(), "line": "12"}]}, schemas.FINDINGS)
        self.assertEqual(len(data["findings"]), 2)
        self.assertEqual(data["findings"][1]["line"], 12)
        with self.assertRaises(gateway.Invalid):
            gateway.clip({"findings": "x" * 28000}, schemas.FINDINGS)
        self.agy_returns(success({"findings": "x" * 28000}))
        r = gateway.run("code", "p", "x", self.root, schemas.FINDINGS, "low", 5)
        self.assertEqual(r.error, "malformed")

    # 5. time budget fits the hook timeout
    def test_timeout_is_clamped(self):
        config.set_value("timeout", 5000)
        self.assertEqual(config.load()["timeout"], config.MAX_TIMEOUT)

    # 6. the same plan is not reviewed twice
    def test_same_plan_text_reviewed_once(self):
        self.agy_returns(success({"findings": []}))
        plan_path = os.path.join(self.root, "docs", "plans", "p.md")
        hooks.on_plan_file({"session_id": "s", "cwd": self.root,
                            "tool_input": {"file_path": plan_path, "content": "1. do it"}})
        hooks.on_exit_plan({"session_id": "s", "cwd": self.root, "tool_input": {"plan": "1. do it"}})
        self.assertEqual(len(self.agy_calls()), 1)

    # 8. parsing gaps
    def test_parsing(self):
        other = self.git_repo("other")
        self.assertEqual(packet.target_repo(f"git -c a.b=c -C {sh(other)} commit -m x", self.root), other)
        secret = os.path.join(self.tmp, "s.md")
        with open(secret, "w") as f:
            f.write("---\nremote_allowed: False\n---\nprivate\n")
        self.assertEqual(packet.decisions(self.root, {"decisions": {self.root: secret}}), "")

    def test_first_review_sends_only_recent_prompts(self):
        for i in range(30):
            hooks.on_prompt({"session_id": "t", "prompt": f"message {i}"})
        words = packet.user_words("t")
        self.assertIn("message 29", words)
        self.assertNotIn("message 5\n", words + "\n")


    def test_a_clean_stop_review_takes_the_pending_commits_with_it(self):
        config.set_value("mode", "thorough")
        self.agy_returns(success({"findings": []}))
        put(self.root, {"small.py": "s = 1\n"})
        self.commit("git add -A && git commit -m x")  # too small alone: kept for the next review
        self.assertEqual(len(state.session("s")["code"][self.root]["pending"]), 1)
        self.edited(self.root, {"new1.py": "a = 1\n" * 25, "new2.py": "b = 2\n" * 25})
        hooks.on_stop({"session_id": "s", "cwd": self.root})
        self.assertEqual(len(self.agy_calls()), 1)
        self.assertEqual(state.session("s")["code"][self.root]["pending"], [])
        self.assertNotIn("not reviewed yet", hooks._inbox("s")[0])


if __name__ == "__main__":
    unittest.main()
