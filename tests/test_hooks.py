import io
import json
import os
import subprocess
import unittest
from contextlib import redirect_stdout

from argon_bridge import cli, config, hooks, packet, state
from tests.helpers import HERE, Env, finding, success, sh

BIG = {"a.py": "x = 1\n" * 25, "b.py": "y = 2\n" * 25}


def write(root, files):
    for rel, text in files.items():
        with open(os.path.join(root, rel), "w") as f:
            f.write(text)
    subprocess.run(["git", "-C", root, "add", "."], check=True)


def commit_inp(root, sid="s", **kw):
    inp = {"session_id": sid, "cwd": root, "tool_name": "Bash",
           "tool_input": {"command": "git commit -m change"}}
    inp.update(kw)
    return inp


class HookTest(Env):
    def setUp(self):
        super().setUp()
        self.root = self.git_repo()
        write(self.root, BIG)
        hooks.on_prompt({"session_id": "s", "prompt": "Make add() add, and make total() skip None."})

    def test_subagent_is_noop_everywhere(self):
        self.agy_returns(success({"findings": [finding()]}))
        sub = {"agent_id": "a1", "agent_type": "general-purpose"}
        self.assertEqual(hooks.on_commit(commit_inp(self.root, **sub)), {})
        self.assertEqual(hooks.on_exit_plan({"session_id": "s", "cwd": self.root, "tool_input": {"plan": "p"}, **sub}), {})
        self.assertEqual(hooks.on_prompt({"session_id": "s", "prompt": "brief", **sub}), {})
        self.assertEqual(self.agy_calls(), [])

    def test_prompt_is_stored_verbatim(self):
        self.assertEqual(state.session("s")["prompts"], ["Make add() add, and make total() skip None."])

    def test_commit_findings_reach_claude_later_with_protocol(self):
        self.agy_returns(success({"findings": [finding(claim="total() still sums None")]}))
        self.assertEqual(hooks.on_commit(commit_inp(self.root)), {})  # the commit is never blocked
        specs, orig = [], hooks.run_job
        hooks.run_job = specs.append  # the review runs later, as in the background
        try:
            self.assertIn("background", self.commit_and_done()["systemMessage"])
        finally:
            hooks.run_job = orig
        hooks.run_job(specs[0])
        ctx = hooks.on_prompt({"session_id": "s", "prompt": "next"})["hookSpecificOutput"]
        self.assertEqual(ctx["hookEventName"], "UserPromptSubmit")
        self.assertIn("UNTRUSTED CLAIMS", ctx["additionalContext"])
        self.assertIn("total() still sums None", ctx["additionalContext"])
        self.assertIn("already committed", ctx["additionalContext"])
        self.assertIn("as a new commit", ctx["additionalContext"])
        self.assertEqual(hooks.on_prompt({"session_id": "s", "prompt": "again"}), {})  # handed over once

    def test_commit_without_findings_passes_with_status(self):
        self.agy_returns(success({"findings": []}))
        out = hooks.on_commit(commit_inp(self.root))
        self.assertNotIn("hookSpecificOutput", out)
        ctx = self.commit_and_done()["hookSpecificOutput"]["additionalContext"]
        self.assertIn("in the background", ctx)
        self.assertIn("Argon: 0 findings", ctx)

    def test_gateway_failure_never_blocks(self):
        self.remove_agy()
        out = hooks.on_commit(commit_inp(self.root))
        self.assertNotIn("hookSpecificOutput", out)
        self.assertIn("not installed", self.commit_and_done()["hookSpecificOutput"]["additionalContext"])

    def test_small_diff_and_off_mode_skip(self):
        small = self.git_repo("small")
        write(small, {"a.py": "x = 2\n"})
        self.assertEqual(hooks.on_commit(commit_inp(small)), {})
        config.set_value("mode", "off")
        self.assertEqual(hooks.on_commit(commit_inp(self.root)), {})
        self.assertEqual(self.agy_calls(), [])

    def test_non_commit_git_command_ignored(self):
        self.assertEqual(hooks.on_commit(commit_inp(self.root, tool_input={"command": "git status"})), {})

    def test_fix_pass_skips_next_commit(self):
        self.agy_returns(success({"findings": [finding()]}))
        hooks.on_commit(commit_inp(self.root))
        self.commit_and_done()
        hooks.on_prompt({"session_id": "s", "prompt": "yes, fix it"})
        write(self.root, {"a.py": "x = 3\n" * 40})
        hooks.on_commit(commit_inp(self.root))
        self.assertNotIn("background", str(self.commit_and_done()))
        self.assertEqual(len(self.agy_calls()), 1)

    def test_fix_pass_survives_failed_commit_attempt(self):
        self.agy_returns(success({"findings": [finding()]}))
        hooks.on_commit(commit_inp(self.root))
        self.commit_and_done()
        hooks.on_prompt({"session_id": "s", "prompt": "yes, fix it"})
        self.assertEqual(hooks.on_commit(commit_inp(self.root)), {})
        hooks.on_commit_done(commit_inp(self.root))  # attempt failed: HEAD did not move
        write(self.root, {"a.py": "x = 3\n" * 40})
        self.assertEqual(hooks.on_commit(commit_inp(self.root)), {})
        subprocess.run(["git", "-C", self.root, "commit", "-qm", "fix"], check=True)
        hooks.on_commit_done(commit_inp(self.root))
        self.assertIsNone(state.session("s").get("code_fix_pass"))
        self.assertEqual(len(self.agy_calls()), 1)

    def test_commands_that_only_mention_git_commit_are_ignored(self):
        for cmd in ('python3 -c "print(\'git commit later\')"', "echo git commit", "grep -r 'git commit' ."):
            self.assertEqual(hooks.on_commit(commit_inp(self.root, tool_input={"command": cmd})), {}, cmd)
        for cmd in ("git commit -m x", f"cd {sh(self.root)} && git add . && git commit -m x",
                    f"git -C {sh(self.root)} commit -m x", "git -c core.x=y commit -m x"):
            self.assertTrue(packet.is_commit(cmd), cmd)
        self.assertEqual(self.agy_calls(), [])

    def test_reviewed_commit_moves_baseline(self):
        self.agy_returns(success({"findings": []}))
        hooks.on_commit(commit_inp(self.root))
        subprocess.run(["git", "-C", self.root, "commit", "-qm", "change"], check=True)
        hooks.on_commit_done(commit_inp(self.root))
        head = subprocess.run(["git", "-C", self.root, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        self.assertEqual(state.session("s")["code"][self.root]["pending"], [])  # reviewed: nothing left over
        with state.owners(self.root) as o:
            self.assertEqual(o["ledger"][head]["sid"], "s")

    def commit_and_done(self):
        subprocess.run(["git", "-C", self.root, "commit", "-qm", "change"], check=True)
        return hooks.on_commit_done(commit_inp(self.root))

    def test_clean_review_is_told_to_claude_after_commit(self):
        self.agy_returns(success({"findings": []}))
        hooks.on_commit(commit_inp(self.root))
        ctx = self.commit_and_done()["hookSpecificOutput"]
        self.assertEqual(ctx["hookEventName"], "PostToolUse")
        self.assertIn("Argon: 0 findings", ctx["additionalContext"])
        self.assertEqual(hooks.on_commit_done(commit_inp(self.root)), {})  # told once

    def test_skipped_review_is_told_to_claude_after_commit(self):
        self.remove_agy()
        hooks.on_commit(commit_inp(self.root))
        self.assertIn("skipped", self.commit_and_done()["hookSpecificOutput"]["additionalContext"])

    def test_a_commit_whose_post_hook_never_ran_is_reviewed_with_the_next(self):
        self.agy_returns(success({"findings": []}))
        hooks.on_commit(commit_inp(self.root))
        subprocess.run(["git", "-C", self.root, "commit", "-qm", "change"], check=True)  # PostToolUse never ran
        write(self.root, {"a.py": "x = 2\n" + "x = 1\n" * 24})
        self.assertEqual(hooks.on_commit(commit_inp(self.root)), {})  # its window starts where the last one did
        self.assertIn("Argon: 0 findings", str(self.commit_and_done()))
        self.assertIn("b.py", self.last_packet())
        write(self.root, {"a.py": "x = 3\n" + "x = 1\n" * 24})
        hooks.on_commit(commit_inp(self.root))
        self.assertEqual(self.commit_and_done(), {})  # small, not reviewed; told once

    def test_clean_plan_review_is_told_to_claude(self):
        self.agy_returns(success({"findings": []}))
        plan = {"session_id": "s", "cwd": self.root, "tool_input": {"plan": "1. fix add"}}
        self.assertIn("Argon: 0 findings", hooks.on_exit_plan(plan)["systemMessage"])
        ctx = hooks.on_exit_plan_done(plan)["hookSpecificOutput"]
        self.assertEqual(ctx["hookEventName"], "PostToolUse")
        self.assertIn("Argon: 0 findings", ctx["additionalContext"])
        self.assertEqual(hooks.on_exit_plan_done(plan), {})
        out = hooks.on_plan_file({"session_id": "s", "cwd": self.root,
                                  "tool_input": {"file_path": f"{self.root}/docs/plans/p.md", "content": "2. fix total"}})
        self.assertIn("Argon: 0 findings", out["hookSpecificOutput"]["additionalContext"])

    def test_exit_plan_reads_plan_and_denies_on_findings(self):
        self.agy_returns(success({"findings": [finding(kind="plan_gap", claim="plan ignores total()")]}))
        out = hooks.on_exit_plan({"session_id": "s", "cwd": self.root, "tool_input": {"plan": "1. fix add"}})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        packet_arg = self.agy_calls()[0][1]
        self.assertIn("review packet", packet_arg)
        # second presentation of the plan is the fix pass
        self.assertEqual(hooks.on_exit_plan({"session_id": "s", "cwd": self.root, "tool_input": {"plan": "2"}}), {})

    def test_plan_file_only_matching_glob(self):
        self.agy_returns(success({"findings": [finding(kind="plan_gap")]}))
        other = {"session_id": "s", "cwd": self.root, "tool_input": {"file_path": f"{self.root}/notes.md", "content": "x"}}
        self.assertEqual(hooks.on_plan_file(other), {})
        plan = {"session_id": "s", "cwd": self.root,
                "tool_input": {"file_path": f"{self.root}/docs/plans/p.md", "content": "1. fix add"}}
        out = hooks.on_plan_file(plan)
        self.assertEqual(out["decision"], "block")

    def review_now(self):
        with redirect_stdout(io.StringIO()):
            cli.main(["review", "--repo", self.root])

    def test_commit_reuses_review_of_same_diff(self):
        self.agy_returns(success({"findings": [finding(claim="total() still sums None")]}))
        self.review_now()
        # `review` already handed the findings to Claude, so the commit starts no second review
        hooks.on_commit(commit_inp(self.root))
        self.assertEqual(self.commit_and_done(), {})
        self.assertEqual(len(self.agy_calls()), 1)

    def test_commit_reviews_again_when_diff_changed_after_review(self):
        self.agy_returns(success({"findings": []}), success({"findings": []}))
        self.review_now()
        write(self.root, {"a.py": "x = 3\n" * 40})
        hooks.on_commit(commit_inp(self.root))
        self.assertNotIn("reused", self.commit_and_done()["systemMessage"])
        self.assertEqual(len(self.agy_calls()), 2)

    def test_stop_only_in_thorough(self):
        self.agy_returns(success({"findings": [finding()]}))
        stop = {"session_id": "s", "cwd": self.root, "stop_hook_active": False}
        self.edited(self.root, BIG)
        self.assertEqual(hooks.on_stop(stop), {})
        config.set_value("mode", "thorough")
        self.assertEqual(hooks.on_stop({**stop, "stop_hook_active": True}), {})
        self.assertIn("background", hooks.on_stop(stop)["systemMessage"])  # started, turn not held
        self.assertEqual(hooks.on_stop(stop)["decision"], "block")  # next Stop hands the findings over
        self.assertEqual(hooks.on_stop(stop), {})  # same change is not reviewed twice
        self.assertEqual(len(self.agy_calls()), 1)


class CliHookTest(Env):
    def test_bin_runs_hook_and_never_crashes(self):
        exe = [os.path.join(os.path.dirname(HERE), "bin", "argon-bridge")]
        if os.name == "nt":  # Claude Code runs hooks with Git Bash on Windows
            exe = [r"C:\Program Files\Git\bin\bash.exe", sh(exe[0])]
        p = subprocess.run([*exe, "hook-commit"], input="not json", capture_output=True, text=True)
        self.assertEqual(p.returncode, 0)
        self.assertIn("argon-bridge error", json.loads(p.stdout)["systemMessage"])
        p = subprocess.run([*exe, "hook-prompt"], input=json.dumps({"session_id": "z", "prompt": "hi"}),
                           capture_output=True, text=True)
        self.assertEqual((p.returncode, p.stdout), (0, ""))


if __name__ == "__main__":
    unittest.main()
