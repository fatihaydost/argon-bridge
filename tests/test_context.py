import os
import unittest

from argon_bridge import context
from tests.helpers import Env


class ContextTest(Env):
    def setUp(self):
        super().setUp()
        self.root = self.git_repo()

    def write_tmp(self, name, text):
        p = os.path.join(self.tmp, name)
        with open(p, "w") as f:
            f.write(text)
        return p

    def test_remote_allowed_false_never_included(self):
        ok = self.write_tmp("rules.md", "# Rules\nuse English\n")
        secret = self.write_tmp("private.md", "---\nremote_allowed: false\n---\nsecret plan\n")
        text, skipped = context.build([ok, secret, "/nope.md"], self.root)
        self.assertIn("use English", text)
        self.assertNotIn("secret plan", text)
        self.assertEqual(skipped, [secret, "/nope.md"])

    def test_large_file_is_truncated_not_skipped(self):
        big = self.write_tmp("big.md", "x" * 150_000)
        text, skipped = context.build([big], self.root)
        self.assertEqual(skipped, [])
        self.assertIn("truncated", text)
        self.assertLess(len(text), 110_000)

    def test_notes_go_into_the_packet_once(self):
        context.add_note(self.root, "run tests with discover -s tests")
        context.add_note(self.root, "run tests with discover -s tests")
        text, _ = context.build([], self.root)
        self.assertEqual(text.count("run tests with discover -s tests"), 1)
        self.assertIn("# Argon notes (mistakes to avoid)", text)

    def test_notes_keep_the_newest_40(self):
        for i in range(45):
            context.add_note(self.root, f"note {i}")
        notes = context.notes(self.root)
        self.assertEqual(len(notes), 40)
        self.assertEqual(notes[-1], "note 44")
        self.assertNotIn("note 4", notes)


if __name__ == "__main__":
    unittest.main()
