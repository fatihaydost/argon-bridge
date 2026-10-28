import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))


def sh(path):
    """A path as it appears in a shell command: Claude's Bash on Windows is Git Bash, which uses /."""
    return path.replace(os.sep, "/")


class Env(unittest.TestCase):
    """Isolated XDG dirs and a PATH whose `agy` is tests/fake_agy."""

    agy = True

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())  # macOS: /var -> /private/var, as git reports it
        self.saved = dict(os.environ)
        os.environ["XDG_CONFIG_HOME"] = os.path.join(self.tmp, "cfg")
        os.environ["XDG_STATE_HOME"] = os.path.join(self.tmp, "st")
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir)
        if self.agy and os.name == "nt":  # Windows runs it through Python as agy.py (compat.agy_cmd)
            shutil.copy(os.path.join(HERE, "fake_agy"), os.path.join(bindir, "agy.py"))
            os.environ["PATHEXT"] = os.environ.get("PATHEXT", ".EXE") + ";.PY"
        elif self.agy:
            os.symlink(os.path.join(HERE, "fake_agy"), os.path.join(bindir, "agy"))
        if os.name == "nt":
            tools = [os.path.dirname(shutil.which("git")), os.path.dirname(sys.executable)]
        else:  # this python and git only, never their folders: a real agy may live next to them (~/.local/bin)
            os.symlink(sys.executable, os.path.join(bindir, "python3"))
            os.symlink(shutil.which("git"), os.path.join(bindir, "git"))
            tools = []
        os.environ["PATH"] = os.pathsep.join([bindir, *tools, "/usr/bin", "/bin"])
        self.args_file = os.path.join(self.tmp, "agy_args.jsonl")
        os.environ["FAKE_AGY_ARGS"] = self.args_file
        os.environ["ARGON_REVIEWER_FOREGROUND"] = "1"  # background reviews run inline in tests
        os.environ["CLAUDE_CODE_SESSION_ID"] = "s"  # tests run inside a Claude session too: never the real one
        os.environ["HOME"] = os.environ["USERPROFILE"] = self.tmp  # never the user's ~/.gemini (Windows reads USERPROFILE)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.saved)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def agy_returns(self, *outs):
        path = os.path.join(self.tmp, "agy_outs.json")  # a file: Windows caps one variable at 32767 characters
        with open(path, "w") as f:
            json.dump(list(outs), f)
        os.environ["FAKE_AGY_OUTS_FILE"] = path

    def remove_agy(self):
        for name in ("agy", "agy.py"):
            p = os.path.join(self.tmp, "bin", name)
            if os.path.exists(p):
                os.remove(p)
        if shutil.which("agy"):  # the test would call the real agy (quota, network)
            self.skipTest(f"a real agy is on the test PATH: {shutil.which('agy')}")

    def agy_calls(self):
        if not os.path.exists(self.args_file):
            return []
        with open(self.args_file) as f:
            return [json.loads(line) for line in f]

    def last_packet(self):
        """Text of the review packet the last `agy` call was pointed at."""
        with open(self.args_file + ".packet") as f:
            return f.read()

    def edited(self, root, files, sid="s"):
        """Writes `files` as Claude's file tools do, through their PostToolUse hook (the Stop review reads these)."""
        from argon_bridge import hooks
        for rel, text in files.items():
            with open(os.path.join(root, rel), "w") as f:
                f.write(text)
            hooks.on_file_edit({"session_id": sid, "cwd": root, "tool_input": {"file_path": os.path.join(root, rel)}})

    def git_repo(self, name="repo", files=None, commit=True):
        root = os.path.join(self.tmp, name)
        os.makedirs(root)
        git = lambda *a: subprocess.run(["git", "-C", root, *a], check=True, capture_output=True)  # noqa: E731
        git("init", "-q")
        git("config", "user.email", "t@t")
        git("config", "user.name", "t")
        for rel, text in (files or {"a.py": "x = 1\n"}).items():
            p = os.path.join(root, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f:
                f.write(text)
        if commit:
            git("add", ".")
            git("commit", "-qm", "init")
        return root


def run_cli(*argv):
    from argon_bridge import cli
    with redirect_stdout(io.StringIO()):
        return cli.main(list(argv))


def run_cli_output(*argv):
    from argon_bridge import cli
    out = io.StringIO()
    with redirect_stdout(out):
        cli.main(list(argv))
    return out.getvalue()


def success(structured):
    return {"status": "SUCCESS", "response": json.dumps(structured), "structured_output": structured,
            "usage": {"total_tokens": 10}}


def finding(**kw):
    base = {"claim": "add subtracts", "file": "a.py", "line": 1, "evidence": "return a - b",
            "severity": "high", "kind": "bug"}
    base.update(kw)
    return base


def phase(title, command="python3 -m unittest discover -s tests", expect="OK"):
    return {"title": title, "goal": f"goal of {title}", "check": {"command": command, "expect": expect}}


def report(status="done", question=None, conversation="c1", **kw):
    """An agy answer whose structured output is a valid PHASE_REPORT."""
    data = {"status": status, "summary": f"phase {status}", "changes": [], "commands_run": [], "failures": [], **kw}
    if question:
        data["question"] = question
    return {**success(data), "conversation_id": conversation}


def fake_job(root, conversation=None, phases=None):
    from argon_bridge import jobs
    job = jobs.create("s", root, "demo", phases or [phase("one")], [], [])
    job["conversation"] = conversation
    jobs.save(job)
    return job


def cwd_of(argv):
    return argv[-1][len("@cwd="):]
