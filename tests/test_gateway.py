import contextlib
import os
import time
import unittest
from unittest import mock

from argon_bridge import config, framing, gateway, permissions, prompts, schemas, state
from argon_bridge import compat
from tests.helpers import HERE, Env, cwd_of, fake_job, finding, phase, report, success

FIXTURES = os.path.join(HERE, "fixtures")  # real agy 1.4.7 stream-json output, shortened, paths anonymized


def run(**kw):
    args = dict(kind="code", prompt="review", packet="PACKET", cwd=os.getcwd(),
                schema=schemas.FINDINGS, effort="medium", timeout=5)
    args.update(kw)
    return gateway.run(**args)


class GatewayTest(Env):
    def test_reviews_run_inside_the_secret_guard(self):
        root = self.git_repo(files={"a.py": "x", ".env": "S=1"})
        seen = []
        orig = permissions.hold

        @contextlib.contextmanager
        def spy(rules, cwd=None):
            seen.append(rules)
            self.assertEqual(cwd, root)
            yield ["--project", "p1"]
        permissions.hold = spy
        try:
            self.agy_returns(success({"findings": []}))
            gateway.run("code", "p", "x", root, schemas.FINDINGS, "low", 30)
        finally:
            permissions.hold = orig
        self.assertEqual(seen, [{"deny": permissions.secret_rules(root)}])
        self.assertIn(f"read_file({os.path.join(root, '.env')})", seen[0]["deny"])
        argv = self.agy_calls()[0]
        self.assertEqual(argv[argv.index("--project") + 1], "p1")

    def test_success_returns_structured_output(self):
        self.agy_returns(success({"findings": [finding()]}))
        r = run()
        self.assertTrue(r.ok)
        self.assertEqual(r.data["findings"][0]["file"], "a.py")
        argv = self.agy_calls()[0]
        self.assertIn("--mode", argv)
        self.assertEqual(argv[argv.index("--mode") + 1], "plan")
        self.assertEqual(argv[argv.index("--model") + 1], "gemini-4-argon")
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertIn("--add-dir", argv)
        self.assertNotIn("PACKET", " ".join(argv))  # packet travels as a file, not argv

    def test_output_is_clipped_locally(self):
        many = [finding(claim="x" * 1000) for _ in range(12)]
        self.agy_returns(success({"findings": many}))
        r = run()
        self.assertEqual(len(r.data["findings"]), 8)
        self.assertEqual(len(r.data["findings"][0]["claim"]), 300)

    def test_quota_sets_cooldown_and_next_call_skips(self):
        self.agy_returns({"status": "ERROR", "error": "RESOURCE_EXHAUSTED: quota exceeded"})
        self.assertEqual(run().error, "quota")
        self.assertGreater(state.cooldown_until(), 0)
        r = run()
        self.assertEqual(r.error, "cooldown")
        self.assertEqual(len(self.agy_calls()), 1)

    def test_login_error(self):
        self.agy_returns({"status": "ERROR", "error": "not authenticated, please sign in"})
        self.assertEqual(run().error, "not_logged_in")

    def test_not_installed(self):
        self.remove_agy()
        self.assertEqual(run().error, "not_installed")

    def test_timeout(self):
        os.environ["FAKE_AGY_SLEEP"] = "3"
        self.agy_returns(success({"findings": []}))
        r = gateway.run(kind="code", prompt="p", packet="x", cwd=os.getcwd(), schema=schemas.FINDINGS,
                        effort="low", timeout=1, grace=0)
        self.assertEqual(r.error, "timeout")

    def test_empty_retries_once_with_lower_effort(self):
        self.agy_returns({"status": "SUCCESS", "response": ""})
        r = run(effort="medium")
        self.assertEqual(r.error, "empty")
        calls = self.agy_calls()
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][calls[1].index("--effort") + 1], "low")

    def test_prose_without_structure_is_malformed(self):
        self.agy_returns({"status": "SUCCESS", "response": "Looks good to me!"})
        self.assertEqual(run().error, "malformed")
        self.assertEqual(len(self.agy_calls()), 2)  # asked once more for JSON only

    def test_every_call_is_logged(self):
        self.agy_returns(success({"findings": []}))
        run(kind="plan")
        entry = state.recent_log(1)[0]
        self.assertEqual(entry["kind"], "plan")
        self.assertIsNone(entry["error"])
        self.assertIn("duration", entry)
        self.assertEqual(entry["usage"], {"total_tokens": 10})


class WorkTest(Env):
    def setUp(self):
        super().setUp()
        self.root = self.git_repo()
        config.set_value("manage_agy_permissions", "true")

    def test_no_guard_no_run(self):
        config.set_value("manage_agy_permissions", "false")
        r = self.work(fake_job(self.root))
        self.assertEqual(r.error, "no_guard")
        self.assertEqual(self.agy_calls(), [])

    def test_worker_rules_go_into_the_run_project(self):
        self.agy_returns(report("done"))
        job = fake_job(self.root)
        gateway.work(job, job["phases"][0], "p", "x", "low", 60, {"allow": ["command(ls)"], "deny": ["command(gh)"]},
                     ["--add-dir", "/skills"])
        args = self.agy_calls()[0]
        self.assertTrue(args[args.index("--project") + 1].startswith("argon-bridge-"))
        self.assertIn("/skills", args)

    def work(self, job, **kw):
        return gateway.work(job, job["phases"][0], "p", "x", "medium", kw.get("timeout", 60), {"deny": []}, [],
                            stall=kw.get("stall", 300), on_event=kw.get("on_event"))

    def test_silent_worker_is_stopped_as_stalled_and_leaves_no_process(self):
        os.environ["FAKE_AGY_STALL"] = "30"
        self.agy_returns(report("done"))
        start = time.time()
        r = self.work(fake_job(self.root), stall=1)
        self.assertLess(time.time() - start, 8)
        self.assertEqual(r.error, "stalled")
        self.assertIn("init", r.detail)  # the last event agy printed
        self.assertIn("silent", r.detail)
        self.assertEqual(r.data["conversation_id"], "c1")  # resumable
        self.assertTrue(r.raw)
        with open(self.args_file + ".pids") as f:
            pids = f.read().split()
        deadline = time.time() + 5
        while any(compat.pid_alive(p) for p in pids) and time.time() < deadline:
            time.sleep(0.1)
        self.assertFalse([p for p in pids if compat.pid_alive(p)], "agy or its command still runs")

    @unittest.skipIf(compat.WINDOWS, "agy shares the worker's process tree on Windows")
    def test_a_command_left_running_after_the_answer_is_stopped(self):
        os.environ["FAKE_AGY_LEFTOVER"] = "60"  # holds agy's stdout open
        self.agy_returns(report("done"))
        start = time.time()
        r = self.work(fake_job(self.root))
        self.assertLess(time.time() - start, 15)
        self.assertEqual(r.data["status"], "done")
        with open(self.args_file + ".leftover") as f:
            pid = f.read().strip()
        deadline = time.time() + 5
        while compat.pid_alive(pid) and time.time() < deadline:
            time.sleep(0.1)
        self.assertFalse(compat.pid_alive(pid), "the command agy left running still runs")

    def test_the_latest_event_is_noted_when_agy_goes_quiet(self):
        os.environ["FAKE_AGY_STEP"] = "read_file"
        os.environ["FAKE_AGY_STALL"] = "1.5"
        self.agy_returns(report("done"))
        seen = []
        with mock.patch.object(gateway, "NOTE_EVERY", 100):  # init is noted at once, the step only when quiet
            self.work(fake_job(self.root), on_event=seen.append)
        self.assertIn("read_file RUNNING", [e.get("event") for e in seen])

    def test_running_agy_is_known_while_it_runs(self):
        seen = []
        self.agy_returns(report("done"))
        self.work(fake_job(self.root), on_event=lambda e: seen.append(gateway.RUNNING_AGY))
        self.assertTrue(seen[0])
        self.assertIsNone(gateway.RUNNING_AGY)

    @unittest.skipIf(compat.WINDOWS, "a running command is not seen on Windows (compat.has_children)")
    def test_a_long_command_is_not_a_stall(self):
        os.environ["FAKE_AGY_STALL"] = "4"
        os.environ["FAKE_AGY_COMMAND"] = "1"  # silent for 4 stall periods, but a command runs: a long test
        self.agy_returns(report("done"))
        r = self.work(fake_job(self.root), stall=1)
        self.assertIsNone(r.error)
        self.assertEqual(r.data["status"], "done")
        with open(self.args_file + ".pids") as f:
            pids = f.read().split()
        deadline = time.time() + 5
        while any(compat.pid_alive(p) for p in pids) and time.time() < deadline:
            time.sleep(0.1)
        self.assertFalse([p for p in pids if compat.pid_alive(p)], "agy or its command still runs")

    def test_events_are_reported_while_agy_runs(self):
        seen = []
        self.agy_returns(report("done"))
        r = self.work(fake_job(self.root), on_event=seen.append)
        self.assertEqual(r.data["status"], "done")
        self.assertTrue(any(e.get("agy_pid") for e in seen))
        self.assertTrue(any(e.get("event") == "init" and e.get("last_event") for e in seen))
        self.assertEqual(seen[-1], {"agy_pid": None})  # agy is gone

    def test_denied_read_file_is_reported_with_its_path(self):
        os.environ["FAKE_AGY_STREAM"] = os.path.join(FIXTURES, "agy_stream_readfile_denied.jsonl")
        r = self.work(fake_job(self.root))
        self.assertTrue(r.ok)
        self.assertEqual(r.data["status"], "denied")
        self.assertEqual(r.data["denied"], ["read_file /home/user/.argon-live-probe/notes.txt"])
        self.assertIn("read_file", r.data["summary"])

    def test_denied_command_is_reported_as_agy_showed_it(self):
        os.environ["FAKE_AGY_STREAM"] = os.path.join(FIXTURES, "agy_stream_command_denied.jsonl")
        r = self.work(fake_job(self.root))
        self.assertEqual(r.data["denied"], ["command: ls && python3 -c 'print(41+1)'"])

    def test_denial_without_its_step_is_not_guessed_from_an_earlier_command(self):
        allowed = '{"event":"step_update","step_update":{"step_index":2,"state":"DONE","step_type":"tool",' \
            '"tool_name":"run_command","tool_info":{"parameters":{"CommandLine":"ls"},"output":"a.py"}}}'
        result = '{"event":"result","result":{"status":"SUCCESS","response":"","conversation_id":"c3",' \
            '"denied_actions":[{"action":"command","display_name":"RunCommand"}]}}'
        path = os.path.join(self.tmp, "stream.jsonl")
        with open(path, "w") as f:
            f.write(allowed + "\n" + result + "\n")
        os.environ["FAKE_AGY_STREAM"] = path
        r = self.work(fake_job(self.root))
        self.assertEqual(r.data["denied"], ["command: (not shown)"])  # a subagent's command is not in the stream

    def test_work_runs_in_worktree_with_edit_mode_and_resumes_conversation(self):
        self.agy_returns(report("done", conversation="c1"))
        job = fake_job(self.root, conversation="c1")
        r = self.work(job)
        args = self.agy_calls()[0]
        self.assertIn("accept-edits", args)
        self.assertEqual(args[args.index("--conversation") + 1], "c1")
        self.assertEqual(args[args.index("--output-format") + 1], "stream-json")
        self.assertEqual(cwd_of(args), job["worktree"])
        self.assertEqual(r.data["status"], "done")
        self.assertEqual(r.data["conversation_id"], "c1")
        self.assertEqual(state.recent_log(1)[0]["kind"], "work")

    def test_every_run_of_a_job_uses_the_same_agy_project(self):
        # a resumed agy conversation keeps the project of its first turn (agy-probes.md, worker smoke run)
        self.agy_returns(report("done"))
        job = fake_job(self.root)
        self.work(job)
        job["conversation"] = "c1"
        self.work(job)
        a, b = self.agy_calls()
        self.assertEqual(a[a.index("--project") + 1], "argon-bridge-job-" + job["id"])
        self.assertEqual(a[a.index("--project") + 1], b[b.index("--project") + 1])
        self.assertFalse(os.path.exists(os.path.join(permissions.projects_dir(), f"argon-bridge-job-{job['id']}.json")))

    def test_first_turn_has_no_conversation(self):
        self.agy_returns(report("done"))
        self.work(fake_job(self.root))
        self.assertNotIn("--conversation", self.agy_calls()[0])

    def test_denied_command_is_reported_not_swallowed(self):
        os.environ["FAKE_AGY_DENIED"] = "git push origin main"
        self.agy_returns({"status": "SUCCESS", "response": "", "conversation_id": "c2"})
        r = self.work(fake_job(self.root))
        self.assertTrue(r.ok)
        self.assertEqual(r.data["status"], "denied")
        self.assertEqual(r.data["denied"], ["command: git push origin main"])
        self.assertEqual(r.data["conversation_id"], "c2")
        self.assertEqual(len(self.agy_calls()), 1)  # no retry

    def test_resumed_turn_never_reads_a_stale_report(self):
        stale = report("done", conversation="c1")
        stale["response"] = "I added a line."  # prose now, structured_output still the last turn's
        self.agy_returns(stale)
        r = self.work(fake_job(self.root, conversation="c1"))
        self.assertEqual(r.error, "malformed")

    def test_resumed_turn_reads_a_fenced_json_report(self):
        out = report("done", conversation="c1")
        out["response"] = "Done.\n```json\n" + out["response"] + "\n```\n"
        self.agy_returns(out)
        r = self.work(fake_job(self.root, conversation="c1"))
        self.assertEqual(r.data["status"], "done")

    def test_worker_prompt_asks_for_a_json_report(self):
        job = fake_job(self.root)
        prompt, _ = prompts.worker(job, job["phases"][0], "", None, None)
        self.assertIn("only one JSON object", prompt)
        for key in ("status", "summary", "changes", "commands_run", "failures", "question", "subagents"):
            self.assertIn(f'"{key}"', prompt)

    def test_empty_worker_answer_at_the_time_limit_is_a_timeout(self):
        os.environ["FAKE_AGY_SLEEP"] = "1.2"
        self.agy_returns({"status": "SUCCESS", "response": ""})
        job = fake_job(self.root)
        r = gateway.work(job, job["phases"][0], "p", "x", "medium", 1, {"deny": []}, [])
        self.assertEqual(r.error, "timeout")

    def test_worker_past_its_whole_time_is_stopped_as_timeout(self):
        os.environ["FAKE_AGY_SLEEP"] = "30"
        start = time.time()
        r = gateway._call_once("p", "x", self.root, schemas.PHASE_REPORT, "low", 1, 0, [], mode="accept-edits")
        self.assertLess(time.time() - start, 8)
        self.assertEqual((r.error, r.detail), ("timeout", "no answer within 1s"))

    def test_empty_worker_answer_is_empty(self):
        self.agy_returns({"status": "SUCCESS", "response": ""})
        self.assertEqual(self.work(fake_job(self.root)).error, "empty")

    def test_quota_is_quota(self):
        self.agy_returns({"status": "ERROR", "error": "RESOURCE_EXHAUSTED quota"})
        self.assertEqual(self.work(fake_job(self.root)).error, "quota")

    def test_worker_prompt_has_the_boundaries(self):
        job = fake_job(self.root)
        prompt, pkt = prompts.worker(job, job["phases"][0], "CONTEXT", "fix the test", "use kebab-case")
        for text in ("never deploy, push, publish", "never read secret files", 'status: "question"',
                     "python3 -m unittest discover -s tests", "never prefix them with `cd`",
                     "Never loosen the check", "an earlier phase's files", "If the plan forbids something"):
            self.assertIn(text, prompt)
        for text in ("CONTEXT", "fix the test", "use kebab-case", "goal of one"):
            self.assertIn(text, pkt)

    def test_subagent_rule_in_worker_prompt(self):
        job = fake_job(self.root)
        prompt, _ = prompts.worker(job, job["phases"][0], "", None, None)
        # SUBAGENTS_SAFE = yes (agy-probes.md): deny rules hold inside Argon's own subagents
        self.assertIn("You may use your own subagents for parts of this phase; report what each did in `subagents`.",
                      prompt)


class GatewayOutputTest(Env):
    """Raw output, timeout classification, retry on a broken form, evidence checks."""

    def setUp(self):
        super().setUp()
        self.root = self.git_repo(files={"a.py": "def add(a, b):\n    return a + b\n"})

    def test_raw_output_is_kept_and_logged(self):
        self.agy_returns({"status": "SUCCESS", "response": "RAW-MARKER prose"})
        r = gateway.run("ask", "p", "x", self.root, schemas.OPINION, "low", 30)
        self.assertTrue(r.raw.startswith(os.path.join(state.dir(), "raw") + os.sep))
        with open(r.raw) as f:
            self.assertIn("RAW-MARKER", f.read())
        if os.name != "nt":
            self.assertEqual(os.stat(r.raw).st_mode & 0o777, 0o600)
        self.assertEqual(state.recent_log(1)[0]["raw"], r.raw)

    def test_old_raw_files_are_pruned(self):
        d = os.path.join(state.dir(), "raw")
        os.makedirs(d)
        for i in range(gateway.RAW_KEEP + 5):
            open(os.path.join(d, f"20261001-000000-ask-{i:04}.txt"), "w").close()
        self.agy_returns(success({"findings": []}))
        run(cwd=self.root)
        self.assertEqual(len(os.listdir(d)), gateway.RAW_KEEP)

    def test_empty_answer_close_to_the_limit_is_a_timeout_and_not_retried(self):
        clock = [1000.0]

        def once(*a, **kw):
            clock[0] += 398
            return gateway.Result(False, error="empty", detail="Argon returned nothing")
        with mock.patch.object(gateway, "_call_once", once), mock.patch.object(gateway.time, "time", lambda: clock[0]):
            r = gateway.run("ask", "p", "x", self.root, schemas.OPINION, "low", 400)
        self.assertEqual(r.error, "timeout")
        self.assertEqual(clock[0], 1398)  # one call: a retry would double the wait
        self.assertIn("did not answer in time", framing.status_line(r))

    def test_interrupted_stream_is_a_timeout_and_not_retried(self):
        self.agy_returns({"status": "ERROR", "error": "The stream was interrupted. Please continue the task."})
        r = run(cwd=self.root, timeout=60)
        self.assertEqual(r.error, "timeout")
        self.assertEqual(len(self.agy_calls()), 1)

    def test_malformed_answer_is_retried_once_asking_for_json_only(self):
        self.agy_returns({"status": "SUCCESS", "response": "some prose"}, success({"findings": []}))
        r = run(cwd=self.root, timeout=60)
        self.assertTrue(r.ok)
        calls = self.agy_calls()
        self.assertEqual(len(calls), 2)
        self.assertNotIn("only one JSON object", calls[0][calls[0].index("-p") + 1])
        self.assertIn("only one JSON object", calls[1][calls[1].index("-p") + 1])
        self.assertTrue(state.recent_log(1)[0]["retried"])

    def test_malformed_twice_stays_malformed(self):
        self.agy_returns({"status": "SUCCESS", "response": "some prose"})
        self.assertEqual(run(cwd=self.root, timeout=60).error, "malformed")
        self.assertEqual(len(self.agy_calls()), 2)

    def test_made_up_evidence_is_unverified(self):
        self.agy_returns(success({"findings": [finding(evidence="return a - b"),
                                               finding(evidence="return   a +\n  b")]}))
        made_up, real = run(cwd=self.root).data["findings"]
        self.assertTrue(made_up["unverified"])
        self.assertNotIn("unverified", real)  # whitespace does not count
        text = framing.findings_for_claude({"findings": [made_up]}, "code")
        self.assertIn("evidence not found in file", text)

    def test_evidence_joined_with_an_ellipsis_is_checked_piece_by_piece(self):
        self.agy_returns(success({"findings": [finding(evidence="def add(a, b):\n...\n    return a + b"),
                                               finding(evidence="def add(a, b): … return a * b")]}))
        joined, made_up = run(cwd=self.root).data["findings"]
        self.assertNotIn("unverified", joined)
        self.assertTrue(made_up["unverified"])  # one made-up piece is enough

    def test_files_outside_the_repository_are_not_read(self):
        with open(os.path.join(self.tmp, "outside.txt"), "w") as f:
            f.write("SECRET LINE\n")
        os.symlink(os.path.join(self.tmp, "outside.txt"), os.path.join(self.root, "link.txt"))
        files = ["../outside.txt", os.path.join(self.tmp, "outside.txt"), "link.txt"]
        self.agy_returns(success({"findings": [finding(file=f, evidence="SECRET LINE") for f in files]}))
        for f in run(cwd=self.root).data["findings"]:
            self.assertTrue(f["unverified"], f["file"])

    def test_finding_off_the_changed_lines_is_pre_existing(self):
        pkt = prompts.code_review("words", "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
                                  "@@ -1,1 +1,2 @@\n def add(a, b):\n+    return a + b\n", "")[1]
        self.agy_returns(success({"findings": [finding(line=2, evidence="return a + b"),
                                               finding(line=40, evidence="return a + b"),
                                               finding(file="b.py", line=1, evidence="return a + b")]}))
        changed, far, other = run(cwd=self.root, packet=pkt).data["findings"]
        self.assertNotIn("origin", changed)
        self.assertEqual(far["origin"], "pre-existing")
        self.assertEqual(other["origin"], "pre-existing")

    def test_no_origin_without_a_diff(self):
        self.agy_returns(success({"findings": [finding(line=40, evidence="return a + b")]}))
        self.assertNotIn("origin", run(cwd=self.root, packet="# Plan\n\nstep 1").data["findings"][0])


    def test_no_origin_when_the_diff_was_cut(self):
        big = "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n@@ -0,0 +1,9000 @@\n" + "+y = 2\n" * 30000
        tail = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,2 @@\n def add(a, b):\n+    return a + b\n"
        pkt = prompts.code_review("words", big + tail, "")[1]
        self.agy_returns(success({"findings": [finding(line=2, evidence="return a + b")]}))
        self.assertNotIn("origin", run(cwd=self.root, packet=pkt).data["findings"][0])  # a.py's hunk was cut off


if __name__ == "__main__":
    unittest.main()
