import os
import tempfile
import unittest

from argon_bridge import config, state


class XdgCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = {k: os.environ.get(k) for k in ("XDG_CONFIG_HOME", "XDG_STATE_HOME")}
        os.environ["XDG_CONFIG_HOME"] = os.path.join(self.tmp.name, "cfg")
        os.environ["XDG_STATE_HOME"] = os.path.join(self.tmp.name, "st")

    def tearDown(self):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()


class ConfigTest(XdgCase):
    def test_defaults_without_file(self):
        cfg = config.load()
        self.assertEqual(cfg["mode"], "standard")
        self.assertEqual(cfg["effort"], "medium")
        self.assertEqual(cfg["timeout"], 900)

    def test_set_and_reload(self):
        config.set_value("effort", "high")
        self.assertEqual(config.load()["effort"], "high")
        self.assertTrue(os.path.exists(os.path.join(self.tmp.name, "cfg", "argon-bridge", "config.json")))

    def test_invalid_values_rejected(self):
        with self.assertRaises(ValueError):
            config.set_value("effort", "max")
        with self.assertRaises(ValueError):
            config.set_value("mode", "always")


class StateTest(XdgCase):
    def test_prompts_round_trip(self):
        state.append_prompt("s1", "first")
        state.append_prompt("s1", "second")
        self.assertEqual(state.session("s1")["prompts"], ["first", "second"])
        self.assertEqual(state.session("other"), {"prompts": []})

    def test_repo_state(self):
        state.save_repo("/x/repo", {"last_reviewed_head": "abc"})
        self.assertEqual(state.repo("/x/repo")["last_reviewed_head"], "abc")

    def test_log_and_cooldown(self):
        state.log({"kind": "code", "error": None})
        state.log({"kind": "ask", "error": "quota"})
        recent = state.recent_log(5)
        self.assertEqual([e["kind"] for e in recent], ["code", "ask"])
        self.assertIn("ts", recent[0])
        self.assertEqual(state.cooldown_until(), 0)
        state.set_cooldown(60)
        self.assertGreater(state.cooldown_until(), 0)
        self.assertTrue(state.dir().startswith(os.path.join(self.tmp.name, "st")))

    def test_results_and_jobs_written_in_one_clock_tick_all_stay_in_order(self):
        from unittest import mock
        with mock.patch.object(state.time, "time_ns", lambda: 1):  # Windows' clock ticks in ~16 ms
            for i in range(3):
                state.inbox_put("s", {"n": i})
            paths = {state.new_job({"n": i}) for i in range(3)}
        self.assertEqual([x["n"] for x in state.inbox_take("s")], [0, 1, 2])
        self.assertEqual(len(paths), 3)


if __name__ == "__main__":
    unittest.main()
