"""The guard gives each agy run its own permission rules in a project file and leaves agy's own files alone."""
import json
import os
import pathlib
import unittest

from argon_bridge import config, permissions, state
from tests.helpers import Env


def settings_path():
    return os.path.join(os.path.expanduser("~"), ".gemini", "antigravity-cli", "settings.json")


def read_settings():
    with open(settings_path()) as f:
        return json.load(f)


def write_settings(data):
    os.makedirs(os.path.dirname(settings_path()), exist_ok=True)
    with open(settings_path(), "w") as f:
        json.dump(data, f)


class NoProjectFileTest(Env):
    """When the per-run project file cannot be written there is no guard; agy's global settings are never edited."""

    def setUp(self):
        super().setUp()
        config.set_value("manage_agy_permissions", "true")
        os.makedirs(os.path.join(self.tmp, ".gemini"))
        open(os.path.join(self.tmp, ".gemini", "config"), "w").close()  # a file where the projects dir would go

    def test_no_guard_and_the_global_file_untouched(self):
        write_settings({"model": "x"})
        with open(settings_path()) as f:
            before = f.read()
        with permissions.hold({"deny": ["command(sudo)"]}) as on:
            self.assertIsNone(on)
            with open(settings_path()) as f:
                self.assertEqual(f.read(), before)
        self.assertEqual(state.recent_log(1)[0]["kind"], "permissions")

    def test_config_value_is_a_bool(self):
        self.assertIs(config.load()["manage_agy_permissions"], True)
        self.assertIs(config.DEFAULTS["manage_agy_permissions"], False)
        with self.assertRaises(ValueError):
            config.set_value("manage_agy_permissions", "yes")


class PerRunTest(Env):
    """agy reads rules from a project file named with --project, so a run needs no edit of the global file."""

    def setUp(self):
        super().setUp()
        os.environ["HOME"] = os.environ["USERPROFILE"] = self.tmp
        config.set_value("manage_agy_permissions", "true")

    def projects(self):
        d = permissions.projects_dir()
        return sorted(os.listdir(d)) if os.path.isdir(d) else []

    def test_rules_go_to_a_project_file_named_on_the_command_line(self):
        write_settings({"a": 1})
        with permissions.hold({"deny": ["read_file(/x/.env)"]}) as args:
            self.assertEqual(args[0], "--project")
            with open(os.path.join(permissions.projects_dir(), args[1] + ".json")) as f:
                project = json.load(f)
            self.assertEqual(project["id"], args[1])
            self.assertEqual(project["permissionGrants"], {"permissionGrants": {"deny": ["read_file(/x/.env)"]}})
            self.assertEqual(read_settings(), {"a": 1})
        self.assertEqual(self.projects(), [])
        self.assertEqual(read_settings(), {"a": 1})

    def test_the_folders_own_project_rules_still_apply(self):
        root = self.git_repo()
        os.makedirs(permissions.projects_dir())
        for name, folder, deny in (("mine", root, "read_file(/mine)"), ("other", os.path.abspath("/elsewhere"), "read_file(/other)")):  # abspath: C:\\elsewhere on Windows
            with open(os.path.join(permissions.projects_dir(), name + ".json"), "w") as f:
                json.dump({"id": name, "projectResources": {"resources": [{"gitFolder": {"folderUri": pathlib.Path(folder).as_uri()}}]},
                           "settings": {"fileAccessPolicy": "X"},
                           "permissionGrants": {"permissionGrants": {"deny": [deny], "allow": ["command(make)"]}}}, f)
        with permissions.hold({"deny": ["read_file(/x)"]}, cwd=os.path.join(root, "sub")) as args:
            with open(os.path.join(permissions.projects_dir(), args[1] + ".json")) as f:
                project = json.load(f)
        self.assertEqual(project["permissionGrants"]["permissionGrants"],
                         {"deny": ["read_file(/mine)", "read_file(/x)"], "allow": ["command(make)"]})
        self.assertEqual(project["settings"], {"fileAccessPolicy": "X"})

    def test_folder_without_a_project_keeps_the_default_projects_rules(self):
        os.makedirs(permissions.projects_dir())
        with open(os.path.join(permissions.projects_dir(), "default-cli-project.json"), "w") as f:
            json.dump({"id": "default-cli-project", "projectResources": {},
                       "permissionGrants": {"permissionGrants": {"deny": ["read_file(/d)"]}}}, f)
        with permissions.hold({"deny": ["read_file(/x)"]}, cwd=self.tmp) as args:
            with open(os.path.join(permissions.projects_dir(), args[1] + ".json")) as f:
                self.assertEqual(json.load(f)["permissionGrants"]["permissionGrants"]["deny"], ["read_file(/d)", "read_file(/x)"])

    def test_odd_project_files_never_break_the_run(self):
        os.makedirs(permissions.projects_dir())
        for name, data in (("a", {"permissionGrants": ["x"], "projectResources": {"resources": ["file:///"]}}),
                           ("default-cli-project", {"permissionGrants": {"permissionGrants": "deny"}})):
            with open(os.path.join(permissions.projects_dir(), name + ".json"), "w") as f:
                json.dump(data, f)
        with permissions.hold({"deny": ["read_file(/x)"]}, cwd=self.tmp) as args:
            self.assertEqual(args[0], "--project")
            with open(os.path.join(permissions.projects_dir(), args[1] + ".json")) as f:
                self.assertEqual(json.load(f)["permissionGrants"]["permissionGrants"], {"deny": ["read_file(/x)"]})

    def test_relative_root_gives_absolute_rules(self):
        root = self.git_repo(files={".env": "S=1"})
        old = os.getcwd()
        os.chdir(self.tmp)
        try:
            rules = permissions.secret_rules("repo")
        finally:
            os.chdir(old)
        self.assertIn(f"read_file({os.path.join(root, '.env')})", rules)

    def test_disabled_writes_nothing(self):
        config.set_value("manage_agy_permissions", "false")
        with permissions.hold({"deny": ["read_file(/x)"]}) as args:
            self.assertIsNone(args)
        self.assertEqual(self.projects(), [])

    def test_crashed_run_leaves_no_project_behind(self):
        permissions._enter({"deny": ["read_file(/x)"]})
        name = self.projects()[0]
        os.rename(os.path.join(permissions.projects_dir(), name),
                  os.path.join(permissions.projects_dir(), "argon-bridge-999999-0.json"))
        with open(os.path.join(permissions.projects_dir(), "someone-else.json"), "w") as f:
            f.write("{}")
        permissions.cleanup_stale()
        self.assertEqual(self.projects(), ["someone-else.json"])

    def test_a_dead_jobs_project_file_is_cleaned(self):
        os.makedirs(permissions.projects_dir())
        for job in ("live", "gone"):
            open(os.path.join(permissions.projects_dir(), f"argon-bridge-job-{job}.json"), "w").close()
        state.mark_running("m1", {"sid": "s", "kind": "phase", "root": self.tmp, "timeout": 60, "job_id": "live"})
        state.mark_running("m2", {"sid": "s", "kind": "phase", "root": self.tmp, "timeout": 60, "job_id": "gone"},
                           pid=999999)  # killed: its marker is still there, not reaped yet
        permissions.cleanup_stale()
        self.assertEqual(self.projects(), ["argon-bridge-job-live.json"])

    def test_secret_rules_name_each_secret_file(self):
        root = self.git_repo(files={"a.py": "x", ".env": "S=1", "sub/.env": "S=2", ".env.example": "S=",
                                    "keys/id.pem": "k", "node_modules/m/.env": "S=3", ".envrc": "S=4",
                                    "deploy/id_ed25519": "k", "app/release.keystore": "k"})
        rules = permissions.secret_rules(root)
        for rel in (".env", "sub/.env", "keys/id.pem", ".envrc", "deploy/id_ed25519", "app/release.keystore"):
            self.assertIn(f"read_file({os.path.join(root, os.path.normpath(rel))})", rules)
        self.assertFalse([r for r in rules if "example" in r or "node_modules" in r or "a.py" in r])
        self.assertEqual(rules[:len(permissions.SECRET_DENY)], permissions.SECRET_DENY)
        for home in ("/.ssh)", "/.config/gh)", "/.git-credentials)", "/.netrc)", "/.docker)", "/.kube)", "/.gnupg)",
                     "/.npmrc)", "/.pypirc)", "/.claude/.credentials.json)"):
            home = home.replace("/", os.sep)  # native separators on every platform
            self.assertTrue(any(r.endswith(home) and os.path.isabs(r[len("read_file("):-1])
                                for r in permissions.SECRET_DENY), home)


if __name__ == "__main__":
    unittest.main()
