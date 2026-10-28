import json
import os
import unittest

from argon_bridge import config, skills
from tests.helpers import Env


def make_skill(base, name, description, folded=False):
    d = os.path.join(base, name)
    os.makedirs(d, exist_ok=True)
    desc = f">\n  {description}\n  more words" if folded else description
    with open(os.path.join(d, "SKILL.md"), "w") as f:
        f.write(f"---\nname: {name}\ndescription: {desc}\n---\n\nBody\n")
    return d


class SkillsTest(Env):
    def setUp(self):
        super().setUp()
        self.claude_skills = os.path.join(self.tmp, ".claude", "skills")

    def test_never_list_wins(self):
        make_skill(self.claude_skills, "ponytail", "Lazy senior dev mode")
        make_skill(self.claude_skills, "ship-it", "Deploy the app to production")
        make_skill(self.claude_skills, "vault-memory", "Vault memory")
        given, refused = skills.allowed(["ponytail", "ship-it", "vault-memory", "nope"])
        self.assertEqual([os.path.basename(d) for d in given], ["ponytail"])
        self.assertEqual(sorted(n for n, _ in refused), ["nope", "ship-it", "vault-memory"])

    def test_config_always_adds_never_removes(self):
        make_skill(self.claude_skills, "tdd", "Test first")
        make_skill(self.claude_skills, "style", "House style")
        config.set_value("worker_skills", {"always": ["style"], "never": ["tdd"]})
        given, _ = skills.allowed(["tdd"])
        self.assertEqual([os.path.basename(d) for d in given], ["style"])

    def test_plugin_skills_found_and_own_skill_refused(self):
        plug = os.path.join(self.tmp, "plug")
        make_skill(os.path.join(plug, "skills"), "test-driven-development", "Use when implementing any feature",
                   folded=True)
        pdir = os.path.join(self.tmp, ".claude", "plugins")
        os.makedirs(pdir)
        with open(os.path.join(pdir, "installed_plugins.json"), "w") as f:
            json.dump({"version": 2, "plugins": {"sp@m": [{"installPath": plug}],
                                                 "argon-bridge@t": [{"installPath": os.path.dirname(skills.OWN_SKILLS)}]}}, f)
        found = skills.installed()
        self.assertEqual(found["test-driven-development"], os.path.join(plug, "skills", "test-driven-development"))
        self.assertEqual(found["sp:test-driven-development"], found["test-driven-development"])
        given, refused = skills.allowed(["test-driven-development", "argon-bridge"])
        self.assertEqual(len(given), 1)
        self.assertEqual([n for n, _ in refused], ["argon-bridge"])

    def test_wrapped_and_plain_multiline_descriptions_are_read(self):
        for name, desc in (("wrapped", "Helps with notes\n  and can deploy to production"),
                           ("plain", "\n  Can push builds to the store")):
            d = os.path.join(self.claude_skills, name)
            os.makedirs(d)
            with open(os.path.join(d, "SKILL.md"), "w") as f:
                f.write(f"---\nname: {name}\ndescription: {desc}\n---\nBody\n")
        given, refused = skills.allowed(["wrapped", "plain"])
        self.assertEqual(given, [])
        self.assertEqual(sorted(n for n, _ in refused), ["plain", "wrapped"])

    def test_never_list_matches_plugin_and_bare_names(self):
        plug = os.path.join(self.tmp, "plug")
        make_skill(os.path.join(plug, "skills"), "tidy", "Tidy code")
        pdir = os.path.join(self.tmp, ".claude", "plugins")
        os.makedirs(pdir)
        with open(os.path.join(pdir, "installed_plugins.json"), "w") as f:
            json.dump({"version": 2, "plugins": {"pp@m": [{"installPath": plug}]}}, f)
        config.set_value("worker_skills", {"always": [], "never": ["pp:tidy"]})
        self.assertEqual(skills.allowed(["tidy"])[0], [])
        config.set_value("worker_skills", {"always": [], "never": ["tidy"]})
        self.assertEqual(skills.allowed(["pp:tidy"])[0], [])

    def test_project_skills_come_from_the_target_repo_not_the_cwd(self):
        repo = self.git_repo()
        make_skill(os.path.join(repo, ".claude", "skills"), "house", "House rules")
        cwd = os.path.join(self.tmp, "vault")
        make_skill(os.path.join(cwd, ".claude", "skills"), "vault-only", "Vault thing")
        old = os.getcwd()
        os.chdir(cwd)
        try:
            found = skills.installed(repo)
        finally:
            os.chdir(old)
        self.assertIn("house", found)
        self.assertNotIn("vault-only", found)
        config.set_value("private_roots", [repo])
        self.assertNotIn("house", skills.installed(repo))

    def test_delivery_copies_into_the_worktree_without_touching_its_own_skills(self):
        # SKILL_DELIVERY = worktree_exclude: a --add-dir holding .agents/ becomes agy's command folder (agy-probes.md)
        d = make_skill(self.claude_skills, "ponytail", "Lazy")
        own = make_skill(self.claude_skills, "tdd", "Test first")
        wt = os.path.join(self.tmp, "wt")
        make_skill(os.path.join(wt, ".agents", "skills"), "tdd", "The repo's own")
        self.assertEqual(skills.deliver([d, own], wt), [".agents/skills/ponytail"])
        self.assertTrue(os.path.isfile(os.path.join(wt, ".agents", "skills", "ponytail", "SKILL.md")))
        with open(os.path.join(wt, ".agents", "skills", "tdd", "SKILL.md")) as f:
            self.assertIn("The repo's own", f.read())
        self.assertEqual(skills.deliver([], wt), [])

if __name__ == "__main__":
    unittest.main()
