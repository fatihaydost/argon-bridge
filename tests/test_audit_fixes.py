"""Regressions for the defects found in the Ponytail audit (23.10.2026)."""
import os
import stat
import subprocess
import time

from argon_bridge import config, framing, gateway, hooks, packet, schemas, state
from tests.helpers import Env, finding, success, sh


def git(root, *a):
    return subprocess.run(["git", "-C", root, *a], check=True, capture_output=True, text=True).stdout.strip()


def put(root, files):
    for rel, text in files.items():
        with open(os.path.join(root, rel), "w") as f:
            f.write(text)


class AuditFixTest(Env):
    def setUp(self):
        super().setUp()
        self.root = self.git_repo()
        hooks.on_prompt({"session_id": "s", "prompt": "do the thing"})

    # 1. a branch switch must not pull the other branch's changes into the diff
    def test_diff_after_branch_switch_ignores_other_branch(self):
        main = git(self.root, "rev-parse", "--abbrev-ref", "HEAD")
        git(self.root, "checkout", "-qb", "feature")
        put(self.root, {"big.py": "y = 2\n" * 300})
        git(self.root, "add", ".")
        git(self.root, "commit", "-qm", "feature work")
        reviewed = git(self.root, "rev-parse", "HEAD")
        git(self.root, "checkout", "-q", main)
        put(self.root, {"a.py": "x = 2\n"})
        git(self.root, "add", "a.py")
        diff = packet.code_diff(self.root, since=reviewed)
        self.assertNotIn("big.py", diff)
        self.assertIn("a.py", diff)

    # 2. a Stop review must not tell Claude to retry a commit nobody made
    def test_stop_findings_do_not_ask_for_a_commit(self):
        config.set_value("mode", "thorough")
        self.agy_returns(success({"findings": [finding()]}))
        self.edited(self.root, {"n1.py": "a = 1\n" * 25, "n2.py": "b = 2\n" * 25})
        hooks.on_stop({"session_id": "s", "cwd": self.root})
        out = hooks.on_stop({"session_id": "s", "cwd": self.root})
        self.assertEqual(out["decision"], "block")
        self.assertNotIn("commit", out["reason"].split("<<<")[0])
        self.assertIn("Then continue.", out["reason"])

    # 3. `git add <file> && git commit` reviews only what will be committed
    def test_add_of_one_file_leaves_other_files_out(self):
        self.agy_returns(success({"findings": []}))
        put(self.root, {"keep.py": "k = 1\n" * 40, "scratch.py": "s = 1\n" * 40})
        adds = packet.staging("git add keep.py && git commit -m x", self.root, self.root)
        diff = packet.code_diff(self.root, None, adds)
        self.assertIn("keep.py", diff)
        self.assertNotIn("scratch.py", diff)
        self.assertEqual(git(self.root, "diff", "--cached", "--name-only"), "")  # real index untouched

    def test_commit_all_stages_tracked_files_only(self):
        put(self.root, {"a.py": "x = 9\n", "untracked.py": "u = 1\n"})
        diff = packet.code_diff(self.root, None, packet.staging("git commit -am x", self.root, self.root))
        self.assertIn("a.py", diff)
        self.assertNotIn("untracked.py", diff)

    def test_add_from_subdirectory_and_failed_add(self):
        os.makedirs(os.path.join(self.root, "sub"))
        put(self.root, {"sub/one.py": "o = 1\n", "two.py": "t = 1\n"})
        sub = os.path.join(self.root, "sub")
        diff = packet.code_diff(self.root, None, packet.staging("git add one.py && git commit -m x", sub, self.root))
        self.assertIn("sub/one.py", diff)
        self.assertNotIn("two.py", diff)
        # a path git cannot resolve falls back to the whole tree instead of reviewing nothing
        diff = packet.code_diff(self.root, None, packet.staging("git add $NOPE && git commit -m x", self.root, self.root))
        self.assertIn("two.py", diff)

    # 5. session files are private and old ones are deleted
    def test_session_files_private_and_pruned(self):
        path = os.path.join(state.dir(), "sessions", "s.json")
        if os.name != "nt":  # Windows has no Unix modes
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        old = time.time() - (state.SESSION_DAYS + 1) * 86400
        os.utime(path, (old, old))
        hooks.on_prompt({"session_id": "new", "prompt": "hi"})
        self.assertFalse(os.path.exists(path))
        self.assertTrue(os.path.exists(os.path.join(state.dir(), "sessions", "new.json")))

    # 6. findings that do not fit the form are counted, not silently lost
    def test_dropped_findings_are_reported(self):
        self.agy_returns(success({"findings": [finding(), finding(severity="major")]}))
        result = gateway.run("code", "p", "packet", self.root, schemas.FINDINGS, "low", 30)
        self.assertEqual(len(result.data["findings"]), 1)
        self.assertIn("1 item(s) dropped", framing.status_line(result))

    # 7. env prefixes and full paths are still commits
    def test_commit_with_env_prefix_or_full_path(self):
        for cmd in ("GIT_AUTHOR_NAME=x git commit -m y", "/usr/bin/git commit -m y",
                    f"cd {sh(self.root)} && FOO=1 /usr/bin/git -C . commit -m y"):
            self.assertTrue(packet.is_commit(cmd), cmd)
            self.assertEqual(packet.target_repo(cmd, self.root), self.root, cmd)
        self.assertFalse(packet.is_commit("mygit commit"))

    # 8. (found live) commit text inside quotes or heredocs is not a commit
    def test_quoted_or_heredoc_commit_text_is_not_a_commit(self):
        heredoc = "cat > t.py <<'EOF'\nx = \"git add a && git commit -m x\"\nEOF\npython3 -m unittest"
        for cmd in (heredoc, "echo 'cd a && git commit -m x'", "python3 - <<'EOF'\nrun(\"git commit\")\nEOF"):
            self.assertFalse(packet.is_commit(cmd), cmd)
        self.assertTrue(packet.is_commit("cat x > y\ngit commit -m z"))
        self.assertTrue(packet.is_commit("(cd sub && git commit -m z)"))
        claude_style = "git add a.py && git commit -m \"$(cat <<'EOF'\nfix: x\n\nAli'nin notu\nEOF\n)\""
        self.assertTrue(packet.is_commit(claude_style))
        self.assertEqual(packet.staging(claude_style, self.root, self.root), [(self.root, ["a.py"])])
        self.assertTrue(packet.is_commit("cat > n.md <<EOF\nAli'nin\nEOF\ngit commit -m z"))

    # 9. (found live) an empty answer that took the whole timeout is a timeout, not "empty"
    def test_empty_answer_at_timeout_is_a_timeout(self):
        self.agy_returns({"status": "SUCCESS", "response": ""})
        os.environ["FAKE_AGY_SLEEP"] = "1.1"
        result = gateway.run("code", "p", "packet", self.root, schemas.FINDINGS, "low", 1)
        self.assertEqual(result.error, "timeout")
        self.assertEqual(len(self.agy_calls()), 1)  # no retry


class ArgonFindingsTest(Env):
    """Regressions for Argon's own review of the audit fixes (23.10.2026)."""

    def setUp(self):
        super().setUp()
        self.root = self.git_repo()
        hooks.on_prompt({"session_id": "s", "prompt": "do the thing"})

    # A1. `-a` is read from the commit's own options only
    def test_commit_all_only_from_commit_options(self):
        for cmd in ("git commit -am x", "git commit --all -m x", "git commit -a", "git commit -vam x"):
            self.assertTrue(packet.commit_all(cmd), cmd)
        for cmd in ("git commit -m x && ls -la", "git commit -m '-all of it'", "git commit -m -a",
                    "git commit -m x --author 'A <a@b>'", "git commit -ma"):
            self.assertFalse(packet.commit_all(cmd), cmd)

    # A2. `git -C X` applies to that call only
    def test_dash_c_does_not_leak_into_later_calls(self):
        other = self.git_repo("other")
        put(self.root, {"mine.py": "m = 1\n"})
        cmd = f"git -C {sh(other)} status && git add mine.py && git commit -m x"
        self.assertEqual(packet.target_repo(cmd, self.root), self.root)
        self.assertEqual(packet.staging(cmd, self.root, self.root), [(self.root, ["mine.py"])])

    # A3. every `git add` before the commit counts
    def test_all_adds_before_commit(self):
        put(self.root, {"one.py": "o = 1\n", "two.py": "t = 1\n", "three.py": "x = 1\n"})
        cmd = "git add one.py && git add two.py && git commit -m x && git add three.py"
        diff = packet.code_diff(self.root, None, packet.staging(cmd, self.root, self.root))
        self.assertIn("one.py", diff)
        self.assertIn("two.py", diff)
        self.assertNotIn("three.py", diff)

    # A4. Stop does not re-review a change `review` already handed to Claude
    def test_stop_reuses_manual_review(self):
        config.set_value("mode", "thorough")
        self.edited(self.root, {"n1.py": "a = 1\n" * 25, "n2.py": "b = 2\n" * 25})
        diff = packet.code_diff(self.root, None, [(self.root, ["-A"])])
        hooks.remember_review(self.root, diff, gateway.Result(ok=True, data={"findings": [finding()]}, duration=1))
        self.assertEqual(hooks.on_stop({"session_id": "s", "cwd": self.root}), {})
        self.assertEqual(self.agy_calls(), [])

    # A5. the timeout check is in _call, which both the first try and the retry go through
    def test_retry_at_timeout_is_a_timeout(self):
        calls = []
        real = gateway._call_once

        def slow_second(*a, **kw):
            calls.append(1)
            if len(calls) == 2:
                os.environ["FAKE_AGY_SLEEP"] = "1.1"
            return real(*a, **kw)
        self.agy_returns({"status": "SUCCESS", "response": ""})
        gateway._call_once = slow_second
        try:
            result = gateway.run("code", "p", "packet", self.root, schemas.FINDINGS, "low", 1)
        finally:
            gateway._call_once = real
        self.assertEqual(len(calls), 2)  # the first empty answer came in time: retried
        self.assertEqual(result.error, "timeout")

    # A6. a `cd` into a dir that does not exist does not crash the review
    def test_add_in_missing_dir_falls_back(self):
        put(self.root, {"two.py": "t = 1\n"})
        cmd = "cd nope && git add x.py && git commit -m y"
        diff = packet.code_diff(self.root, None, packet.staging(cmd, self.root, self.root))
        self.assertIn("two.py", diff)

    # B4. a value after a combined flag ending in a value-taking letter is not an option
    def test_commit_all_after_combined_value_flag(self):
        cases = {'git commit -qm "-a"': False, "git commit -qm --all": False,
                 "git commit -m -m -a": True, "git commit -vF msg -a": True}
        for cmd, expected in cases.items():
            self.assertEqual(packet.commit_all(cmd), expected, cmd)
        self.assertTrue(packet.commit_all("git commit -qam x"))
        self.assertFalse(packet.commit_all("git commit -mfoo"))

    # B5. `);` after a subshell is a separator, and a `cd` inside it ends with it
    def test_subshell_parsing(self):
        cmd = "(cd sub && git add a.py); git commit -m x"
        calls = list(packet._git_calls(cmd, "/r"))
        self.assertEqual(calls, [(os.path.join("/r", "sub"), "add", ["a.py"]), ("/r", "commit", ["-m", "x"])])
