import inspect
import os
import subprocess
import unittest

from argon_bridge import packet, prompts, state
from tests.helpers import Env, sh


def stage(root, rel, text):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(text)
    subprocess.run(["git", "-C", root, "add", rel], check=True)


class PacketTest(Env):
    def test_target_repo_from_command(self):
        a = self.git_repo("a")
        b = self.git_repo("b")
        os.makedirs(os.path.join(b, "sub"))
        self.assertEqual(packet.target_repo(f"cd {sh(b)} && git commit -m x", a), b)
        self.assertEqual(packet.target_repo(f"git -C {sh(b)}/sub commit -m 'x y'", a), b)
        self.assertEqual(packet.target_repo("git commit -m x", os.path.join(b, "sub")), b)
        self.assertIsNone(packet.target_repo("git commit -m x", self.tmp))

    def test_commit_all_flag(self):
        self.assertTrue(packet.commit_all("git commit -am 'x'"))
        self.assertTrue(packet.commit_all("git commit --all -m x"))
        self.assertFalse(packet.commit_all("git commit -m 'add a feature'"))

    def test_diff_first_commit_and_no_head(self):
        fresh = self.git_repo("fresh", commit=False)
        subprocess.run(["git", "-C", fresh, "add", "."], check=True)
        self.assertIn("a.py", packet.code_diff(fresh))
        root = self.git_repo("r")
        stage(root, "b.py", "y = 2\n")
        self.assertIn("b.py", packet.code_diff(root, since="deadbeef"))

    def test_diff_since_reviewed_head_includes_later_commits(self):
        root = self.git_repo("r")
        head = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        stage(root, "sub_agent.py", "z = 3\n")
        subprocess.run(["git", "-C", root, "commit", "-qm", "by subagent"], check=True)
        stage(root, "c.py", "w = 4\n")
        diff = packet.code_diff(root, since=head)
        self.assertIn("sub_agent.py", diff)
        self.assertIn("c.py", diff)

    def test_is_small(self):
        one = "diff --git a/a.py b/a.py\n" + "+x\n" * 10
        two = "diff --git a/a.py b/a.py\n" + "+x\n" * 20 + "diff --git a/b.py b/b.py\n" + "+y\n" * 20
        docs = "".join(f"diff --git a/d{i}.md b/d{i}.md\n" + "+t\n" * 50 for i in range(5))
        self.assertTrue(packet.is_small(one))
        self.assertTrue(packet.is_small(docs))
        self.assertFalse(packet.is_small(two))

    def test_decisions_respects_remote_allowed(self):
        root = self.git_repo("r")
        ok = os.path.join(self.tmp, "ok.md")
        secret = os.path.join(self.tmp, "secret.md")
        with open(ok, "w") as f:
            f.write("---\ntitle: x\n---\nMVP has 200 labs; no pagination on purpose.\n")
        with open(secret, "w") as f:
            f.write("---\nremote_allowed: false\n---\nprivate\n")
        self.assertIn("200 labs", packet.decisions(root, {"decisions": {root: ok}}))
        self.assertEqual(packet.decisions(root, {"decisions": {root: secret}}), "")
        self.assertEqual(packet.decisions(root, {}), "")

    def test_decisions_key_with_tilde_matches(self):
        root = self.git_repo("r")
        ok = os.path.join(self.tmp, "ok.md")
        with open(ok, "w") as f:
            f.write("no pagination on purpose\n")
        os.environ["HOME"] = os.environ["USERPROFILE"] = self.tmp
        self.assertIn("pagination", packet.decisions(root, {"decisions": {"~/r": ok}}))

    def test_user_words_since_last_review(self):
        state.append_prompt("s", "old request")
        packet.mark_reviewed("s")
        state.append_prompt("s", "make total skip None")
        self.assertEqual(packet.user_words("s"), "make total skip None")

    def test_prompts_carry_user_words_and_no_claude_reasoning(self):
        prompt, pkt = prompts.code_review("Fix add() please", "diff --git a/a.py b/a.py\n", "")
        self.assertIn("Fix add() please", pkt)
        self.assertIn("Never run shell commands", prompt)
        for fn in (prompts.code_review, prompts.plan_review, prompts.ask):
            self.assertFalse({"reasoning", "claude", "summary"} & set(inspect.signature(fn).parameters))


if __name__ == "__main__":
    unittest.main()
