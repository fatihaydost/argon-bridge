"""Portable helpers: these run on Windows CI too."""
import os
import subprocess
import sys
import tempfile
import unittest

from argon_bridge import compat


class CompatTest(unittest.TestCase):
    def test_pid_alive_never_stops_the_process(self):
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            self.assertTrue(compat.pid_alive(p.pid))
            self.assertIsNone(p.poll())  # still running: on Windows os.kill(pid, 0) would have terminated it
        finally:
            p.kill()
            p.wait()
        self.assertFalse(compat.pid_alive(p.pid))
        self.assertTrue(compat.pid_alive("not-a-pid"))  # malformed ids are left alone

    def test_unlink_removes_the_link_not_the_target(self):
        with tempfile.TemporaryDirectory() as d:
            src_dir, src_file = os.path.join(d, "deps"), os.path.join(d, "env")
            os.makedirs(os.path.join(src_dir, "pkg"))
            with open(src_file, "w") as f:
                f.write("SECRET=1\n")
            for src, name in ((src_dir, "deps-link"), (src_file, "env-link")):
                dst = os.path.join(d, name)
                compat.link(src, dst)
                self.assertTrue(os.path.exists(dst))
                compat.unlink(dst)
                self.assertFalse(os.path.lexists(dst))
            self.assertTrue(os.path.isdir(os.path.join(src_dir, "pkg")))
            self.assertTrue(os.path.isfile(src_file))

    def test_broken_link_is_removed_too(self):
        with tempfile.TemporaryDirectory() as d:
            src, dst = os.path.join(d, "deps"), os.path.join(d, "deps-link")
            os.makedirs(src)
            compat.link(src, dst)
            os.rmdir(src)  # the target is gone
            compat.unlink(dst)
            self.assertFalse(os.path.lexists(dst))

    def test_only_python_scripts_run_through_python(self):
        with tempfile.TemporaryDirectory() as d:
            for first, through_python in (("#!/usr/bin/env python3", True), ("#!/usr/bin/env node", False),
                                          ("#!/bin/sh", False), ("\x7fELF", False)):
                path = os.path.join(d, "agy")
                with open(path, "w") as f:
                    f.write(first + "\n")
                orig = compat.agy_path
                compat.agy_path = lambda: path
                try:
                    self.assertEqual(compat.agy_cmd(), [sys.executable, path] if through_python else [path], first)
                finally:
                    compat.agy_path = orig

    def test_detached_options_fit_the_platform(self):
        opts = compat.detached()
        self.assertIn("creationflags" if compat.WINDOWS else "start_new_session", opts)

    @unittest.skipIf(compat.WINDOWS, "agy shares the worker's process tree on Windows")
    def test_stop_agy_stops_agy_and_its_commands_and_nothing_else(self):
        with tempfile.TemporaryDirectory() as d:
            agy = os.path.join(d, "agy")
            with open(agy, "w") as f:  # an `agy` that runs a command, in a session of its own as gateway starts it
                f.write(f"#!{sys.executable}\nimport subprocess, sys, time\n"
                        f"subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\ntime.sleep(60)\n")
            os.chmod(agy, 0o755)
            a = subprocess.Popen([agy], start_new_session=True)
            other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
            try:
                import time
                time.sleep(0.5)
                kids = subprocess.run(["pgrep", "-P", str(a.pid)], capture_output=True, text=True).stdout.split()
                compat.stop_agy(other.pid)  # a reused pid that is not agy: left alone
                compat.stop_agy(a.pid)
                a.wait(timeout=5)
                self.assertIsNone(other.poll())
                deadline = time.time() + 5
                while any(compat.pid_alive(k) for k in kids) and time.time() < deadline:
                    time.sleep(0.1)
                self.assertTrue(kids)
                self.assertFalse([k for k in kids if compat.pid_alive(k)])
            finally:
                for p in (a, other):
                    p.kill()
                    p.wait()

    def test_watchdog_stops_agy_first(self):
        calls = []
        from unittest import mock
        with mock.patch.object(compat, "kill_tree", lambda pid: calls.append("self")):
            compat.watchdog(0.05, first=lambda: calls.append("agy"))
            import time
            time.sleep(0.5)
        self.assertEqual(calls, ["agy", "self"])


if __name__ == "__main__":
    unittest.main()
