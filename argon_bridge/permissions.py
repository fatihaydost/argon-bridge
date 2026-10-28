"""The only place that changes agy's permission rules (the user turns this on: `manage_agy_permissions`).

A run's rules go into a project file of its own, ~/.gemini/config/projects/argon-bridge-<pid>-<n>.json, that
`agy --project <id>` reads; nothing else changes (docs/agy-probes.md). When that file cannot be
written there is no guard for the run; agy's own settings files are never edited."""
from __future__ import annotations

import contextlib
import itertools
import json
import os
from urllib.parse import urlparse
from urllib.request import url2pathname

from . import compat, config, state

KINDS = ("allow", "ask", "deny")
# agy read_file rules are path prefixes: no `~`, no globs, no regex (agy-probes.md)
_SECRET_HOME = ("~/.ssh", "~/.aws", "~/.config/gcloud", "~/.config/gh", "~/.git-credentials", "~/.netrc", "~/.docker",
                "~/.kube", "~/.gnupg", "~/.npmrc", "~/.pypirc", "~/.claude/.credentials.json")
SECRET_DENY = [f"read_file({os.path.normpath(os.path.expanduser(p))})" for p in _SECRET_HOME]
_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".tox", "dist", "build", ".next", "target"}
_PREFIX = "argon-bridge-"
_count = itertools.count()


def _is_secret(name):
    if name == ".env" or (name.startswith(".env.") and not name.endswith((".example", ".sample", ".template"))):
        return True
    return name == ".envrc" or name.startswith(("id_rsa", "id_ed25519", "id_ecdsa")) \
        or name.endswith((".pem", ".key", ".p12", ".pfx", ".keystore", ".jks"))


def is_secret_path(path):
    """A file Argon must never see: a secret-looking name (also behind a link) or a secret place in the home folder."""
    real = os.path.realpath(path)
    homes = [os.path.realpath(os.path.expanduser(p)) for p in _SECRET_HOME]
    return _is_secret(os.path.basename(path)) or _is_secret(os.path.basename(real)) \
        or any(real == h or real.startswith(h + os.sep) for h in homes)


def secret_rules(root):
    """SECRET_DENY plus one deny rule per secret-looking file under `root`."""
    rules, seen = list(SECRET_DENY), 0
    for top, dirs, files in os.walk(os.path.abspath(root)):  # agy reads a relative rule from its own cwd
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for path in (os.path.join(top, n) for n in files if _is_secret(n)):
            rules += [f"read_file({path})"] + ([f"read_file({os.path.realpath(path)})"] if os.path.islink(path) else [])
        seen += len(files)
        if seen > 50000:  # shortcut: a huge tree is only partly scanned, upgrade if a real repo hits this
            break
    return rules


def projects_dir():
    return os.path.normpath(os.path.expanduser("~/.gemini/config/projects"))


def _alive(key):
    """Whether the run behind a project file key still runs: `<pid>-<n>`, or `job-<id>` while a phase marker
    of that job names a live process (state.running_jobs)."""
    if key.startswith("job-"):
        return any(m.get("job_id") == key[4:] and compat.pid_alive(m.get("pid")) for m in state.running_jobs())
    return compat.pid_alive(key.split("-")[0])


def cleanup_stale():
    """Removes the project files of runs that died holding rules (SIGKILL, reboot). Never raises."""
    try:
        for name in os.listdir(projects_dir()) if os.path.isdir(projects_dir()) else []:
            if name.startswith(_PREFIX) and name.endswith(".json") and not _alive(name[len(_PREFIX):-5]):
                with contextlib.suppress(OSError):
                    os.remove(os.path.join(projects_dir(), name))
    except Exception as e:  # the guard must never block a review
        state.log({"kind": "permissions", "error": f"{type(e).__name__}: {e}"})


def _folders(project):
    for r in (project.get("projectResources") or {}).get("resources") or []:
        uri = (r.get("gitFolder") or {}).get("folderUri") or r.get("folderUri") or ""
        if uri.startswith("file://"):
            yield os.path.realpath(url2pathname(urlparse(uri).path))  # file:///C:/x -> C:\x on Windows


def _own_project(cwd):
    """The project agy would use for `cwd` (deepest folder that holds it, else the default one), or {}:
    `--project` replaces it for the run, so its rules and settings are carried over."""
    best, depth = {}, -1
    cwd = os.path.realpath(cwd) if cwd else None
    for name in os.listdir(projects_dir()) if os.path.isdir(projects_dir()) else []:
        if not name.endswith(".json") or name.startswith(_PREFIX):
            continue
        try:
            with open(os.path.join(projects_dir(), name)) as f:
                p = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(p, dict):
            continue
        if name == "default-cli-project.json" and depth < 0:
            best, depth = p, 0
        for folder in _folders(p) if cwd else []:
            if (cwd == folder or cwd.startswith(folder + os.sep)) and len(folder) > depth:
                best, depth = p, len(folder)
    return best


def _project(key, rules, cwd):
    """Writes the run's project file; returns its path, or None when it cannot be written."""
    pid = _PREFIX + key
    path = os.path.join(projects_dir(), pid + ".json")
    try:
        own = _own_project(cwd)
        grants = dict(((own.get("permissionGrants") or {}).get("permissionGrants") or {}))
        if any(not isinstance(v, list) for v in grants.values()):
            raise ValueError("unexpected permissionGrants")
    except Exception as e:  # an odd user project: run with the plugin's rules only
        state.log({"kind": "permissions", "error": f"own project ignored: {type(e).__name__}: {e}"})
        own, grants = {}, {}
    for k in KINDS:
        grants[k] = list(grants.get(k) or []) + [e for e in rules.get(k, []) if e not in (grants.get(k) or [])]
        if not grants[k]:
            del grants[k]
    project = {k: v for k, v in own.items() if k not in ("id", "name", "projectResources", "permissionGrants")}
    project.update({"id": pid, "name": pid, "projectResources": {}, "permissionGrants": {"permissionGrants": grants}})
    try:
        os.makedirs(projects_dir(), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"  # a job's project id is fixed, its runs may overlap
        with open(tmp, "w") as f:
            json.dump(project, f, indent=2)
        os.replace(tmp, path)
        return path
    except (OSError, TypeError, ValueError) as e:
        state.log({"kind": "permissions", "error": f"project file: {e}; this run has no guard", "guard": False})
        return None


def _enter(rules, cwd=None, name=None):
    """(project file, extra agy args), or None when the guard is off or the file cannot be written."""
    if config.load().get("manage_agy_permissions") is not True:
        return None
    cleanup_stale()
    key = name or f"{os.getpid()}-{next(_count)}"
    project = _project(key, rules, cwd)
    return (project, ["--project", _PREFIX + key]) if project else None


@contextlib.contextmanager
def hold(rules, cwd=None, name=None):
    """Puts `rules` ({"allow": [...], "deny": [...]}) in effect for one agy run and yields the extra agy
    arguments that run needs, or None when the guard is off or failed: the run then goes ahead without it.
    `cwd`: the run's folder, whose own agy project rules are kept. `name`: a fixed project id suffix for runs
    that continue one agy conversation (it keeps the project of its first turn), e.g. "job-<id>". Never raises
    on its own."""
    held = _enter(rules, cwd, name)
    try:
        yield held[1] if held else None
    finally:
        if held:
            with contextlib.suppress(OSError):
                os.remove(held[0])
